# -*- coding: utf-8 -*-
# Target: Odoo 20.0 Community Edition
"""Demo manufacturing orders for the production cost reports.

Self-contained on purpose: this module does not depend on
``l10n_vn_stock_reports``, so it builds its own component with stock and its
own real-time categories rather than borrowing another demo's. Two orders
finish (the cost report and S37-DN get figures), one stays open with its
materials issued (the cost card gets a closing WIP).

Runs once; done manufacturing orders cannot be unlinked, so a rerun reports
itself as already loaded. Marker: any product coded ``VNDEMO-MRP-NVL``.
"""

from odoo import _, api, models
from odoo.exceptions import UserError

_ACCOUNT_FALLBACKS = {
    'valuation_nvl': ('1521', '152'),
    'valuation_tp': ('1551', '155'),
    'input_nvl': ('331',),
    'output_nvl': ('621', '154'),
    'input_tp': ('154',),
    'output_tp': ('632',),
    'labour': ('622', '6271', '642'),
}

#: The demo's assembly team: what an hour costs, and the time one wardrobe
#: takes. Costed on the planned time so the demo needs no clocked hours.
LABOUR_COST_HOUR = 120000.0
MINUTES_PER_UNIT = 30.0
#: Overhead charged per wardrobe (electricity, glue, depreciation share).
EXTRA_UNIT_COST = 50000.0


class VnMrpDemoGenerator(models.AbstractModel):
    _name = 'vn.demo.mrp.generator'
    _description = 'Vietnam VAS Demo Data - Manufacturing'

    @api.model
    def generate(self, company=None):
        company = company or self.env.company
        Product = self.env['product.product']
        if Product.search([('default_code', '=', 'VNDEMO-MRP-NVL')], limit=1):
            return {'company': company.name, 'skipped': True}

        nvl_categ, tp_categ = self._ensure_categories(company)
        component = Product.create({
            'name': 'Gỗ sồi ghép thanh (demo SX)',
            'default_code': 'VNDEMO-MRP-NVL',
            'type': 'consu',
            'is_storable': True,
            'categ_id': nvl_categ.id,
            'standard_price': 400000.0,
        })
        finished = Product.create({
            'name': 'Tủ quần áo 4 cánh (demo SX)',
            'default_code': 'VNDEMO-MRP-TP',
            'type': 'consu',
            'is_storable': True,
            'categ_id': tp_categ.id,
        })

        # Consumption and output post through the production location's
        # valuation account. 154: issue Nợ 154 / Có 152, receipt Nợ 155 / Có 154.
        production = finished.with_company(company).property_stock_production
        if production and not production.valuation_account_id:
            production.valuation_account_id = self._account(company, 'input_tp')

        self._set_stock(company, component, 500.0)
        # One operation, so the orders carry labour: Odoo values the output at
        # materials + workcenter cost + extra unit cost and posts the labour
        # Nợ 154 / Có 622 when the order finishes.
        workcenter = self.env['mrp.workcenter'].create({
            'name': 'Tổ lắp ráp (demo SX)',
            'company_id': company.id,
            'costs_hour': LABOUR_COST_HOUR,
            'expense_account_id': self._account(company, 'labour').id,
        })
        bom = self.env['mrp.bom'].create({
            'product_tmpl_id': finished.product_tmpl_id.id,
            'product_qty': 1,
            'bom_line_ids': [(0, 0, {'product_id': component.id,
                                     'product_qty': 4})],
            'operation_ids': [(0, 0, {
                'name': 'Lắp ráp',
                'workcenter_id': workcenter.id,
                'time_mode': 'manual',
                'time_cycle_manual': MINUTES_PER_UNIT,
                'cost_mode': 'estimated',
            })],
        })

        for quantity in (10.0, 5.0):
            self._run_order(finished, bom, quantity, finish=True)
        # One order stays open with its materials already issued: that is the
        # closing WIP the cost card exists to show.
        self._run_order(finished, bom, 8.0, finish=False)

        return {'company': company.name, 'skipped': False, 'orders': 3}

    # ------------------------------------------------------------------
    def _account(self, company, key):
        Account = self.env['account.account'].with_company(company)
        for prefix in _ACCOUNT_FALLBACKS[key]:
            account = Account.search([
                ('company_ids', 'in', [company.id]),
                ('code', '=like', prefix + '%'),
            ], order='code', limit=1)
            if account:
                return account
        raise UserError(_(
            'No account found for %s; install a chart of accounts or run '
            'the accounting demo first.') % (_ACCOUNT_FALLBACKS[key],))

    def _ensure_categories(self, company):
        journal = self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'general')],
            limit=1)
        Category = self.env['product.category']

        # Odoo 20 dropped the interim input/output accounts on the category
        # and moved the production cost account to the company as the WIP
        # account. 154 is what that account means in the Vietnamese chart:
        # consumption Nợ 154 / Có 152, receipt Nợ 155 / Có 154.
        if not company.account_production_wip_account_id:
            company.account_production_wip_account_id = \
                self._account(company, 'input_tp')

        def category(name, valuation_key, input_key, output_key):
            return Category.create({
                'name': name,
                'property_valuation': 'real_time',
                'property_cost_method': 'average',
                'property_stock_valuation_account_id':
                    self._account(company, valuation_key).id,
                'property_stock_journal': journal.id,
            })

        return (category('VAS demo SX - Nguyên vật liệu',
                         'valuation_nvl', 'input_nvl', 'output_nvl'),
                category('VAS demo SX - Thành phẩm',
                         'valuation_tp', 'input_tp', 'output_tp'))

    def _set_stock(self, company, product, quantity):
        """Set on-hand through the quant inventory flow.

        ``stock.inventory`` left Odoo in 15.0; the counted quantity on the
        quant plus ``action_apply_inventory`` is the supported way since.
        """
        location = self.env['stock.warehouse'].search(
            [('company_id', '=', company.id)], limit=1).lot_stock_id
        # Odoo 20 posts a stock entry only when the location on the other
        # side carries a valuation account. Opening stock comes in from the
        # inventory adjustment location; give it 331, the counterpart the
        # earlier versions' demo printed on S10-DN.
        adjustment = product.with_company(company).property_stock_inventory
        if adjustment and not adjustment.valuation_account_id:
            adjustment.valuation_account_id = self._account(company, 'input_nvl')
        quant = self.env['stock.quant'].with_context(
            inventory_mode=True).create({
                'product_id': product.id,
                'location_id': location.id,
                'inventory_quantity': quantity,
            })
        quant.action_apply_inventory()

    def _run_order(self, finished, bom, quantity, finish):
        # The component moves are computed from the BoM on create, so no Form
        # emulator is needed — and Odoo 20 logs an error whenever business
        # code imports odoo.tests.
        order = self.env['mrp.production'].create({
            'product_id': finished.id,
            'bom_id': bom.id,
            'product_qty': quantity,
        })
        # Set on the order rather than the BoM: the BoM field is 20.0 only,
        # the order's has been there since 17.0.
        order.extra_cost = EXTRA_UNIT_COST
        order.action_confirm()
        order.action_assign()
        if finish:
            order.qty_producing = quantity
            order._set_qty_producing(False)
            order.button_mark_done()
        else:
            # Issue the components without finishing: the raw moves complete,
            # the order stays in progress, and the consumed value is exactly
            # the closing WIP an accountant computes for TK 154.
            for move in order.move_raw_ids:
                # quantity_done became quantity + picked in Odoo 17.
                move.quantity = move.product_uom_qty
                move.picked = True
            order.move_raw_ids._action_done()
        return order
