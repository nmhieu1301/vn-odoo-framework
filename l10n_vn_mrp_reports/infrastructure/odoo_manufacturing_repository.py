# -*- coding: utf-8 -*-
# Target: Odoo 20.0 Community Edition
"""Odoo implementation of ``IManufacturingRepository``.

Cost and quantity both come from the stock moves a manufacturing order owns.
Odoo 20 has no ``stock.valuation.layer``; the value a move took into or out of
stock is ``stock_move.value``. Odoo 19 stores it unsigned and lets ``is_out``
give the direction, Odoo 20 stores an issue negative; the queries below sign
it from ``is_out`` either way, as the layer was:

* components carry ``stock_move.raw_material_production_id``; their value is
  negative because it left stock, so the material cost is the negated sum;
* the finished goods carry ``stock_move.production_id``; their value is what
  Odoo capitalised into account 155.

Only done moves that actually crossed into or out of valued stock count
(``is_in`` / ``is_out``), which is what Odoo itself values.

Taking the output quantity from the same moves as the output value, rather than
from ``qty_produced`` on the order, keeps the two consistent: a unit cost
computed from a quantity and a value that came from different places is a unit
cost that can disagree with both.
"""

from odoo import fields as odoo_fields

from odoo.addons.vn_core.domain.manufacturing.repository import (
    IManufacturingRepository,
)
from odoo.addons.vn_core.dto.common import CurrencyDTO
from odoo.addons.vn_core.dto.inventory import ProductDTO
from odoo.addons.vn_core.dto.manufacturing import ProductionDTO
from odoo.addons.vn_core.infrastructure.base_repository import BaseRepository

#: States an order passes through before it has produced anything final.
OPEN_STATES = ('draft', 'confirmed', 'planned', 'progress', 'to_close')


class OdooManufacturingRepository(IManufacturingRepository, BaseRepository):

    def get_currency(self, company_ids):
        company = self.env['res.company'].browse(list(company_ids))[:1]
        currency = company.currency_id or self.env.company.currency_id
        return CurrencyDTO(
            id=currency.id, name=currency.name,
            rounding=currency.rounding, decimal_places=currency.decimal_places)

    # ------------------------------------------------------------------
    def get_productions(self, manufacturing_filter, done=True):
        domain = [('company_id', 'in', list(manufacturing_filter.company_ids))]

        if done:
            domain.append(('state', '=', 'done'))
            # An order is costed when it finishes, so the period selects on the
            # completion date rather than on when it was planned.
            if manufacturing_filter.date_from:
                domain.append(('date_finished', '>=', odoo_fields.Date.to_string(
                    manufacturing_filter.date_from)))
            domain.append(('date_finished', '<=', '%s 23:59:59' % (
                odoo_fields.Date.to_string(manufacturing_filter.date_to))))
        else:
            # Still open *at the reporting date*: started on or before it, and
            # not finished. Orders opened afterwards are not yet anybody's WIP.
            domain += [
                ('state', 'in', list(OPEN_STATES)),
                ('date_start', '<=', '%s 23:59:59' % (
                    odoo_fields.Date.to_string(manufacturing_filter.date_to))),
            ]

        domain += self._scope_domain(manufacturing_filter)
        return self._to_dtos(self.env['mrp.production'].search(domain))

    def get_productions_open_at(self, manufacturing_filter, at_date):
        """WIP as it stood at the end of ``at_date``.

        ``done=False`` above answers "what is open now, as of the report date";
        this answers the historical question the cost card asks — so an order
        that has since finished still counts if it was unfinished on that date.
        """
        end_of_day = '%s 23:59:59' % odoo_fields.Date.to_string(at_date)
        domain = [
            ('company_id', 'in', list(manufacturing_filter.company_ids)),
            ('date_start', '<=', end_of_day),
            '|', ('state', 'in', list(OPEN_STATES)),
            '&', ('state', '=', 'done'), ('date_finished', '>', end_of_day),
        ] + self._scope_domain(manufacturing_filter)
        return self._to_dtos(self.env['mrp.production'].search(domain))

    @staticmethod
    def _scope_domain(manufacturing_filter):
        domain = []
        if manufacturing_filter.production_ids:
            domain.append(('id', 'in', list(manufacturing_filter.production_ids)))
        if manufacturing_filter.product_ids:
            domain.append(
                ('product_id', 'in', list(manufacturing_filter.product_ids)))
        if manufacturing_filter.category_ids:
            domain.append(('product_id.categ_id', 'child_of',
                           list(manufacturing_filter.category_ids)))
        return domain

    @staticmethod
    def _to_dtos(productions):
        return tuple(
            ProductionDTO(
                id=production.id,
                name=production.name or '',
                product=ProductDTO(
                    id=production.product_id.id,
                    code=production.product_id.default_code or '',
                    name=production.product_id.name or '',
                    uom_name=production.uom_id.name or '',
                    category_name=production.product_id.categ_id.complete_name or '',
                ),
                state=production.state,
                date_finished=(production.date_finished.date()
                               if production.date_finished else None),
                date_started=(production.date_start.date()
                              if production.date_start else None),
            )
            for production in productions
        )

    # ------------------------------------------------------------------
    def get_material_costs(self, production_ids, date_from=None, date_to=None):
        if not production_ids:
            return {}
        self._flush()
        # The consumption date is the date the component move was done, as in
        # the inventory reports.
        bounds, params = '', [tuple(production_ids)]
        if date_from:
            bounds += " AND sm.date >= %s"
            params.append(odoo_fields.Date.to_string(date_from))
        if date_to:
            bounds += " AND sm.date <= %s"
            params.append('%s 23:59:59' % odoo_fields.Date.to_string(date_to))
        self.cr.execute("""
            SELECT sm.raw_material_production_id  AS production_id,
                   COALESCE(SUM(CASE WHEN sm.is_out THEN -ABS(sm.value) ELSE sm.value END), 0.0)
                                                  AS value
              FROM stock_move sm
             WHERE sm.raw_material_production_id IN %%s
               AND sm.state = 'done' AND (sm.is_in OR sm.is_out)%s
          GROUP BY sm.raw_material_production_id
        """ % bounds, params)
        # Components leave stock, so their value is negative. The cost of the
        # order is what left; a component returned to stock comes back
        # positive and reduces it.
        return {row['production_id']: -(row['value'] or 0.0)
                for row in self.cr.dictfetchall()}

    def get_outputs(self, production_ids):
        if not production_ids:
            return {}
        self._flush()
        self.cr.execute("""
            SELECT sm.production_id                   AS production_id,
                   COALESCE(SUM(
                       (SELECT COALESCE(SUM(sml.quantity_product_uom), 0.0)
                          FROM stock_move_line sml
                         WHERE sml.move_id = sm.id AND sml.picked)
                       * CASE WHEN sm.is_out THEN -1 ELSE 1 END), 0.0)
                                                      AS quantity,
                   COALESCE(SUM(CASE WHEN sm.is_out THEN -ABS(sm.value) ELSE sm.value END), 0.0)
                                                  AS value
              FROM stock_move sm
             WHERE sm.production_id IN %s
               AND sm.state = 'done' AND (sm.is_in OR sm.is_out)
          GROUP BY sm.production_id
        """, (tuple(production_ids),))
        return {row['production_id']: (row['quantity'] or 0.0,
                                       row['value'] or 0.0)
                for row in self.cr.dictfetchall()}

    def get_conversion_costs(self, production_ids):
        """Labour and overhead, as ``mrp.production._cal_price`` adds them.

        When an order finishes Odoo values the finished goods at the
        components plus ``mrp.workorder._cal_cost()`` of every operation plus
        ``extra_cost`` per unit produced, and ``_post_labour`` posts the first
        of those (rounded per operation) to the work-in-progress account. Both
        are read from the same sources here, so the three elements add up to
        the value Odoo capitalised.
        """
        result = {}
        for production in self.env['mrp.production'].browse(production_ids):
            currency = production.company_id.currency_id
            labour = sum(currency.round(workorder._cal_cost())
                         for workorder in production.workorder_ids)
            finished = production.move_finished_ids.filtered(
                lambda move: move.product_id == production.product_id
                and move.state == 'done')
            quantity = sum(finished.move_line_ids.filtered('picked').mapped(
                'quantity_product_uom'))
            overhead = currency.round(production.extra_cost * quantity)
            if labour or overhead:
                result[production.id] = (labour, overhead)
        return result

    def _flush(self):
        self.env['stock.move'].flush_model()
        self.env['stock.move.line'].flush_model()
