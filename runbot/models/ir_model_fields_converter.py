from odoo import models

class IrFieldsConverter(models.AbstractModel):
    _inherit = 'ir.fields.converter'

    def _str_to_jsonb(self, model, field, value):
        return self._str_to_json(model, field, value)
