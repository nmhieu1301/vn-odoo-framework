# -*- coding: utf-8 -*-
# Target: Odoo 20.0 Community Edition
"""Odoo implementation of ``IInventoryRepository``.

Odoo 20 has no ``stock.valuation.layer``. The value of a movement now lives
on the ``stock.move`` itself: ``value`` is what the move brought into or took
out of stock; ``is_in`` and ``is_out`` say whether the move crossed
the boundary of the company's valued locations at all. The report values stock
the way Odoo does, so its closing figures are the ones Odoo shows for the date.

**Sign.** Odoo 19 stores ``value`` unsigned and lets ``is_out`` give the
direction; Odoo 20 stores an issue's value negative. The report signs it
from ``is_out`` either way, so receipts add and issues subtract on both.

**Quantity.** A move's quantity is in its own unit of measure, and only the
lines actually picked were valued. The quantity is therefore the sum of
``stock.move.line.quantity_product_uom`` over picked lines, signed like the
value.

**Cost changes.** A change of cost made outside a movement is recorded on
``product.value`` with no move, and Odoo's valuation at a date restarts from
it (``_get_last_product_value``):

* standard cost — the stock is worth its quantity at the price in force on the
  date: the last such change on or before it, or the current cost if there is
  none. Opening stock and every movement are valued at that price, and each
  change inside the period is a value-only line, quantity on hand times the
  difference;
* average cost — the value is reset to the new cost times the quantity then on
  hand, and later movements add their own values. Each change is a value-only
  line for the difference between that reset and the value it replaced.

Both are computed as "target minus what the report already had", so a change
Odoo also wrote into the moves themselves is never counted twice. FIFO
products need neither: their moves carry the valuation.

**Which date a movement belongs to.** ``stock.move.date``, the date the move
was done.
"""

from datetime import timedelta

from odoo import fields as odoo_fields

from odoo.addons.vn_core.domain.inventory.repository import IInventoryRepository
from odoo.addons.vn_core.dto.common import CurrencyDTO
from odoo.addons.vn_core.dto.inventory import (
    InventoryBalanceDTO, InventoryMoveDTO, ProductDTO,
)
from odoo.addons.vn_core.infrastructure.base_repository import BaseRepository

MOVE = 'stock.move'
#: One microsecond: "just before" a timestamp.
INSTANT = timedelta(microseconds=1)
#: The move's value, negative on an issue whichever way Odoo stored it.
SIGNED_VALUE = ('(CASE WHEN "stock_move".is_out THEN -ABS("stock_move".value)'
                ' ELSE "stock_move".value END)')
#: Cost methods whose valuation restarts at a manual cost change.
REPLAYED = ('standard', 'average')


class OdooInventoryRepository(IInventoryRepository, BaseRepository):

    # ------------------------------------------------------------------
    def get_currency(self, company_ids):
        company = self.env['res.company'].browse(list(company_ids))[:1]
        currency = company.currency_id or self.env.company.currency_id
        return CurrencyDTO(
            id=currency.id, name=currency.name,
            rounding=currency.rounding, decimal_places=currency.decimal_places)

    def get_products(self, inventory_filter):
        products = self.env['product.product'].search(
            self._product_domain(inventory_filter))
        return tuple(
            ProductDTO(
                id=product.id,
                code=product.default_code or '',
                name=product.name or '',
                uom_name=product.uom_id.name or '',
                category_name=product.categ_id.complete_name or '',
            )
            for product in products
        )

    # ------------------------------------------------------------------
    def get_opening(self, inventory_filter):
        if not inventory_filter.date_from:
            return {}
        start = odoo_fields.Date.to_string(inventory_filter.date_from)
        rows = self._moves(inventory_filter, before=start)
        balances = {}
        for row in rows:
            balance = balances.setdefault(row['product_id'], [0.0, 0.0])
            balance[0] += row['quantity'] or 0.0
            balance[1] += row['value'] or 0.0

        replay = self._replay(inventory_filter,
                              odoo_fields.Datetime.to_datetime(start) - INSTANT)
        for product_id, state in replay.items():
            balances.setdefault(product_id, [0.0, 0.0])[1] = state['value']
        return {product_id: InventoryBalanceDTO(quantity, value)
                for product_id, (quantity, value) in balances.items()}

    # ------------------------------------------------------------------
    def get_movements(self, inventory_filter):
        lower = (odoo_fields.Date.to_string(inventory_filter.date_from)
                 if inventory_filter.date_from else None)
        # Dates are timestamps, so a bare date here means midnight and
        # silently drops everything that moved during the closing day itself.
        upper = '%s 23:59:59' % odoo_fields.Date.to_string(inventory_filter.date_to)
        low = odoo_fields.Datetime.to_datetime(lower) if lower else None
        high = odoo_fields.Datetime.to_datetime(upper)

        rows = self._moves(inventory_filter, after=lower, until=upper,
                           details=True)
        replay = self._replay(inventory_filter, high, low)
        revalued = {}
        for state in replay.values():
            revalued.update(state['moves'])
        for row in rows:
            if row['id'] in revalued:
                row['value'] = revalued[row['id']]
        for state in replay.values():
            rows.extend(state['changes'])
        # A cost change at the very moment of a move counts after it.
        rows.sort(key=lambda row: (row['move_date'], row['id'] < 0, abs(row['id'])))

        names = {product.id: product.name or '' for product in
                 self.env['product.product'].browse(
                     {row['product_id'] for row in rows})}
        return tuple(self._to_dto(row, names) for row in rows)

    @staticmethod
    def _to_dto(row, names):
        quantity = row['quantity'] or 0.0
        value = row['value'] or 0.0
        reference = row.get('reference') or ''
        # What the layer used to say: "WH/IN/00007 - Gỗ sồi xẻ sấy".
        description = row.get('description') or ' - '.join(
            part for part in (reference, names.get(row['product_id'], ''))
            if part)
        return InventoryMoveDTO(
            id=row['id'],
            date=row['move_date'],
            product_id=row['product_id'],
            quantity=quantity,
            value=value,
            unit_cost=abs(value / quantity) if quantity else 0.0,
            reference=reference,
            description=description,
            partner_name=row.get('partner_name') or '',
            move_id=row['id'] if row['id'] > 0 else None,
            counterpart=row.get('counterpart') or '',
        )

    # ------------------------------------------------------------------
    def _replay(self, inventory_filter, until, period_start=None):
        """Odoo's valuation of standard and average products, replayed.

        -> ``{product_id: {'value', 'moves', 'changes'}}`` as of ``until``:
        the stock value then; the value of each move dated on or after
        ``period_start`` where it differs from ``stock_move.value``
        (standard cost); and the value-only rows of the cost changes dated on
        or after ``period_start``. Each change row's id is the negated
        ``product.value`` id, so it cannot collide with a move.
        """
        histories = self._cost_histories(inventory_filter)
        if not histories:
            return {}
        rows = self._moves(inventory_filter, until=until, product_ids=list(histories))
        by_product = {}
        for row in rows:
            by_product.setdefault(row['product_id'], []).append(row)

        result = {}
        for product_id, history in histories.items():
            events = [(row['move_date'], 0, row) for row in by_product.get(product_id, ())]
            events += [(change['date'], 1, change) for change in history['changes']
                       if change['date'] <= until]
            events.sort(key=lambda event: (event[0], event[1], event[2]['id']))
            quantity = value = 0.0
            moves, changes = {}, []
            for when, kind, item in events:
                in_period = period_start is None or when >= period_start
                if kind == 0:
                    move_qty = item['quantity'] or 0.0
                    if history['method'] == 'standard':
                        move_value = move_qty * history['price'].at(when)
                        if in_period:
                            moves[item['id']] = move_value
                    else:
                        move_value = item['value'] or 0.0
                    quantity += move_qty
                    value += move_value
                    continue
                # Both methods restart from the new cost times what is on hand.
                target = quantity * item['price']
                if in_period and quantity and target != value:
                    changes.append({
                        'id': -item['id'],
                        'move_date': when,
                        'product_id': product_id,
                        'quantity': 0.0,
                        'value': target - value,
                        'description': item['description'],
                    })
                value = target
            if history['method'] == 'standard':
                # Before any change, Odoo values at the current cost.
                value = quantity * history['price'].at(until)
            result[product_id] = {'value': value, 'moves': moves, 'changes': changes}
        return result

    def _cost_histories(self, inventory_filter):
        """Standard and average products, with their manual cost changes."""
        company = (self.env['res.company'].browse(list(inventory_filter.company_ids))[:1]
                   or self.env.company)
        products = self.env['product.product'].with_company(company).search(
            self._product_domain(inventory_filter)).filtered(
                lambda product: product.cost_method in REPLAYED
                and not product.lot_valuated)
        if not products:
            return {}
        changes = {}
        for record in self.env['product.value'].sudo().search([
                ('product_id', 'in', products.ids), ('move_id', '=', False),
                ('lot_id', '=', False), ('company_id', '=', company.id),
        ], order='product_id, date, id'):
            changes.setdefault(record.product_id.id, []).append({
                'id': record.id, 'date': record.date, 'price': record.value,
                'description': record.description or '',
            })
        histories = {}
        for product in products:
            product_changes = changes.get(product.id, [])
            # An average product without a manual change is just its moves.
            if product.cost_method == 'average' and not product_changes:
                continue
            histories[product.id] = {
                'method': product.cost_method,
                'changes': product_changes,
                'price': _PriceHistory(product.standard_price, product_changes),
            }
        return histories

    # ------------------------------------------------------------------
    def _moves(self, inventory_filter, before=None, after=None, until=None,
               product_ids=None, details=False):
        """Valued moves as dicts, oldest first.

        ``before`` is exclusive, ``after`` and ``until`` inclusive. ``details``
        adds the reference, partner and "TK đối ứng".
        """
        move_from, move_where, params = self._move_query(inventory_filter)
        bounds = []
        for clause, value in (('<', before), ('>=', after), ('<=', until)):
            if value:
                bounds.append('"stock_move".date %s %%s' % clause)
                params.append(value)
        if product_ids is not None:
            bounds.append('"stock_move".product_id IN %s')
            params.append(tuple(product_ids) or (0,))

        extra, joins = '', ''
        if details:
            # "TK đối ứng" for S10-DN: the accounts on the other side of the
            # journal entry Odoo posted for the move. The stock account sits on
            # the same side as the value's sign — debit on a receipt, credit on
            # an issue — so the counterpart is whatever the entry carries
            # opposite. Odoo only posts such an entry when a location carries a
            # valuation account (production, inventory adjustment, scrap);
            # receipts and deliveries reach the ledger through their bills and
            # invoices, so those lines print no counterpart.
            extra = """,
                   "stock_move".reference            AS reference,
                   rp.name                           AS partner_name,
                   (SELECT string_agg(
                               DISTINCT aa.code_store->>(aml.company_id::text), ', ')
                      FROM account_move_line aml
                      JOIN account_account aa ON aa.id = aml.account_id
                     WHERE aml.move_id = "stock_move".account_move_id
                       AND CASE WHEN "stock_move".is_out
                                THEN aml.debit ELSE aml.credit END > 0)
                                                     AS counterpart"""
            joins = """
              LEFT JOIN stock_picking sp ON sp.id = "stock_move".picking_id
              LEFT JOIN res_partner rp ON rp.id = sp.partner_id"""
        self.cr.execute("""
            SELECT "stock_move".id                   AS id,
                   "stock_move".date                 AS move_date,
                   "stock_move".product_id           AS product_id,
                   {qty}                             AS quantity,
                   {value}                           AS value{extra}
              FROM {move_from}{joins}
             WHERE {move_where}{bounds}
          ORDER BY "stock_move".date, "stock_move".id
        """.format(qty=self._signed_qty_expr(), value=SIGNED_VALUE,
                   extra=extra, joins=joins,
                   move_from=move_from, move_where=move_where,
                   bounds=''.join(' AND ' + bound for bound in bounds)), params)
        return [dict(row) for row in self.cr.dictfetchall()]

    @staticmethod
    def _signed_qty_expr():
        """Picked quantity in the product's unit, negative on an issue."""
        return """
            (SELECT COALESCE(SUM(sml.quantity_product_uom), 0.0)
               FROM stock_move_line sml
              WHERE sml.move_id = "stock_move".id AND sml.picked)
            * CASE WHEN "stock_move".is_out THEN -1 ELSE 1 END
        """

    @staticmethod
    def _product_domain(inventory_filter):
        domain = [('is_storable', '=', True)]
        if inventory_filter.product_ids:
            domain.append(('id', 'in', list(inventory_filter.product_ids)))
        if inventory_filter.category_ids:
            domain.append(
                ('categ_id', 'child_of', list(inventory_filter.category_ids)))
        return domain

    def _move_query(self, inventory_filter):
        domain = [
            ('company_id', 'in', list(inventory_filter.company_ids)),
            ('state', '=', 'done'),
            '|', ('is_in', '=', True), ('is_out', '=', True),
        ] + [('product_id.%s' % field, operator, value) if field != 'id'
             else ('product_id', operator, value)
             for field, operator, value in self._product_domain(inventory_filter)]
        self.env['stock.move.line'].flush_model()
        model = self.env[MOVE]
        model.flush_model()
        # See OdooLedgerRepository._query_parts: _search() applies the record
        # rules itself.
        query = model._search(domain)
        from_sql, where_sql = query.from_clause, query.where_clause
        # 20.0 dropped SQL.code and SQL.params; _sql_tuple is what
        # odoo/tools/sql.py documents as (code, params, to_flush).
        from_code, from_params, _ = from_sql._sql_tuple
        where_code, where_params, _ = where_sql._sql_tuple
        return (from_code, where_code, list(from_params) + list(where_params))


class _PriceHistory:
    """Standard price in force at a moment, by Odoo's rule.

    The last cost change on or before it; the current cost when there is none
    (``product.product._get_last_product_value``).
    """

    def __init__(self, current, changes):
        self.current = current
        self.changes = changes

    def at(self, when):
        price = None
        for change in self.changes:
            if change['date'] > when:
                break
            price = change['price']
        return self.current if price is None else price
