# -*- coding: utf-8 -*-
# Target: Odoo 20.0 Community Edition
"""Odoo implementation of ``IInventoryRepository``.

Odoo 20 has no ``stock.valuation.layer``. The value of a movement now lives on
the ``stock.move`` itself: ``value`` is what the move brought into or took out
of stock, positive on a receipt and negative on an issue, exactly the sign the
layer used to carry; ``is_in`` and ``is_out`` say whether the move crossed the
boundary of the company's valued locations at all. The report values stock the
way Odoo does, so its closing figures are the ones Odoo shows for the date.

**Quantity.** A move's quantity is in its own unit of measure, and only the
lines actually picked were valued. The quantity is therefore the sum of
``stock.move.line.quantity_product_uom`` over picked lines, signed like the
value.

**Average and FIFO cost.** The moves' ``value`` is the valuation. When such a
product's cost is corrected, Odoo 20 rewrites the value of the receipts still
in stock rather than recording a separate revaluation, so the correction shows
in those receipts and needs no line of its own.

**Standard cost.** For a product at standard cost Odoo 20 ignores the moves'
values: the stock is worth its quantity at the standard price in force on the
date, that price being the last cost change (``product.value`` with no move)
on or before it, or the current cost if there is none. The report follows the
same rule: opening stock and every movement of such a product are valued at
the price of their date, and each cost change inside the period becomes a
value-only line, the quantity on hand times the difference. Opening plus
movements then lands exactly on Odoo's own closing value.

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
        move_from, move_where, move_params = self._move_query(inventory_filter)
        self.cr.execute("""
            SELECT "stock_move".product_id         AS product_id,
                   SUM({qty})                      AS quantity,
                   SUM("stock_move".value)         AS value
              FROM {move_from}
             WHERE {move_where}
               AND "stock_move".date < %s
          GROUP BY "stock_move".product_id
        """.format(qty=self._signed_qty_expr(), move_from=move_from,
                   move_where=move_where), move_params + [start])
        balances = {row['product_id']: [row['quantity'] or 0.0, row['value'] or 0.0]
                    for row in self.cr.dictfetchall()}

        prices = self._standard_prices(inventory_filter)
        before = odoo_fields.Datetime.to_datetime(start) - INSTANT
        for product_id, balance in balances.items():
            if product_id in prices:
                balance[1] = balance[0] * prices[product_id].at(before)
        return {product_id: InventoryBalanceDTO(quantity, value)
                for product_id, (quantity, value) in balances.items()}

    # ------------------------------------------------------------------
    def get_movements(self, inventory_filter):
        lower = (odoo_fields.Date.to_string(inventory_filter.date_from)
                 if inventory_filter.date_from else None)
        # Dates are timestamps, so a bare date here means midnight and
        # silently drops everything that moved during the closing day itself.
        upper = '%s 23:59:59' % odoo_fields.Date.to_string(inventory_filter.date_to)

        move_from, move_where, move_params = self._move_query(inventory_filter)
        bounds, bound_params = '"stock_move".date <= %s', [upper]
        if lower:
            bounds += ' AND "stock_move".date >= %s'
            bound_params.append(lower)

        # "TK đối ứng" for S10-DN: the accounts on the other side of the
        # journal entry Odoo posted for the move. The stock account sits on the
        # same side as the value's sign — debit on a receipt, credit on an
        # issue — so the counterpart is whatever the entry carries opposite.
        # Odoo 20 only posts such an entry when a location carries a valuation
        # account (production, inventory adjustment, scrap); receipts and
        # deliveries reach the ledger through their bills and invoices, so
        # those lines print no counterpart.
        counterpart_expr = """
            (SELECT string_agg(
                        DISTINCT aa.code_store->>(aml.company_id::text), ', ')
               FROM account_move_line aml
               JOIN account_account aa ON aa.id = aml.account_id
              WHERE aml.move_id = "stock_move".account_move_id
                AND CASE WHEN "stock_move".value >= 0
                         THEN aml.credit ELSE aml.debit END > 0)
        """
        self.cr.execute("""
            SELECT "stock_move".id                   AS id,
                   "stock_move".date                 AS move_date,
                   "stock_move".product_id           AS product_id,
                   {qty}                             AS quantity,
                   "stock_move".value                AS value,
                   "stock_move".reference            AS reference,
                   rp.name                           AS partner_name,
                   {counterpart}                     AS counterpart
              FROM {move_from}
              LEFT JOIN stock_picking sp ON sp.id = "stock_move".picking_id
              LEFT JOIN res_partner rp ON rp.id = sp.partner_id
             WHERE {move_where}
               AND {bounds}
        """.format(qty=self._signed_qty_expr(), counterpart=counterpart_expr,
                   move_from=move_from, move_where=move_where, bounds=bounds),
            move_params + bound_params)
        rows = [dict(row) for row in self.cr.dictfetchall()]
        rows = self._apply_standard_prices(inventory_filter, rows, lower, upper)

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
    def _apply_standard_prices(self, inventory_filter, rows, lower, upper):
        """Revalue standard-cost rows at the price of their date.

        Adds one value-only row per cost change inside the period. Its id is
        the negated ``product.value`` id, so it cannot collide with a move.
        """
        prices = self._standard_prices(inventory_filter)
        if prices:
            on_hand = {}
            if lower:
                on_hand = {product_id: balance.quantity for product_id, balance
                           in self.get_opening(inventory_filter).items()}
            for row in rows:
                history = prices.get(row['product_id'])
                if history:
                    row['value'] = (row['quantity'] or 0.0) * history.at(row['move_date'])
            low = odoo_fields.Datetime.to_datetime(lower) if lower else None
            high = odoo_fields.Datetime.to_datetime(upper)
            for product_id, history in prices.items():
                for change in history.changes:
                    if change['date'] > high or (low and change['date'] < low):
                        continue
                    quantity = on_hand.get(product_id, 0.0) + sum(
                        row['quantity'] or 0.0 for row in rows
                        if row['product_id'] == product_id and row['id'] > 0
                        and row['move_date'] <= change['date'])
                    previous = history.at(change['date'] - INSTANT)
                    if not quantity or change['price'] == previous:
                        continue
                    rows.append({
                        'id': -change['id'],
                        'move_date': change['date'],
                        'product_id': product_id,
                        'quantity': 0.0,
                        'value': quantity * (change['price'] - previous),
                        'description': change['description'],
                    })
        # A cost change at the very moment of a move counts after it.
        return sorted(rows, key=lambda row: (row['move_date'], row['id'] < 0,
                                             abs(row['id'])))

    def _standard_prices(self, inventory_filter):
        """``{product_id: _PriceHistory}`` for the standard-cost products."""
        company = (self.env['res.company'].browse(list(inventory_filter.company_ids))[:1]
                   or self.env.company)
        products = self.env['product.product'].with_company(company).search(
            self._product_domain(inventory_filter)).filtered(
                lambda product: product.cost_method == 'standard'
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
        return {product.id: _PriceHistory(product.standard_price,
                                          changes.get(product.id, []))
                for product in products}

    # ------------------------------------------------------------------
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
    """Standard price in force at a moment, by Odoo 20's rule.

    The last cost change on or before it; the current cost when there is none
    (``product.product._get_last_product_value`` and ``_run_standard``).
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
