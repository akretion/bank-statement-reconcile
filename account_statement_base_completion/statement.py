# -*- coding: utf-8 -*-
##############################################################################
#
#    Author: Nicolas Bessi, Joel Grand-Guillaume
#    Copyright 2011-2012 Camptocamp SA
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################
# TODO replace customer supplier by package constant
import traceback
import sys
import logging

from collections import defaultdict
import re
from tools.translate import _
from openerp.osv import osv, orm, fields
from openerp.tools import DEFAULT_SERVER_DATETIME_FORMAT
from operator import attrgetter
import datetime

_logger = logging.getLogger(__name__)

class ErrorTooManyPartner(Exception):
    """
    New Exception definition that is raised when more than one partner is matched by
    the completion rule.
    """
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


class AccountStatementProfil(orm.Model):
    """
    Extend the class to add rules per profile that will match at least the partner,
    but it could also be used to match other values as well.
    """

    _inherit = "account.statement.profile"

    _columns = {
        # @Akretion : For now, we don't implement this features, but this would probably be there:
        # 'auto_completion': fields.text('Auto Completion'),
        # 'transferts_account_id':fields.many2one('account.account', 'Transferts Account'),
        # => You can implement it in a module easily, we design it with your needs in mind
        # as well !

        'rule_ids': fields.many2many(
            'account.statement.completion.rule',
            string='Related statement profiles',
            rel='as_rul_st_prof_rel'),
    }

    def _get_callable(self, cr, uid, profile, context=None):
        if isinstance(profile, (int, long)):
            prof = self.browse(cr, uid, profile, context=context)
        else:
            prof = profile
        # We need to respect the sequence order
        sorted_array = sorted(prof.rule_ids, key=attrgetter('sequence'))
        return tuple((x.function_to_call for x in sorted_array))

    def _find_values_from_rules(self, cr, uid, calls, line, context=None):
        """
        This method will execute all related rules, in their sequence order,
        to retrieve all the values returned by the first rules that will match.

        :param int/long st_line: read of the concerned account.bank.statement.line
        :return:
            A dict of value that can be passed directly to the write method of
            the statement line or {}
           {'partner_id': value,
            'account_id' : value,

            ...}
        """
        if context is None:
            context = {}
        if not calls:
            calls = self._get_callable(cr, uid, id, context=context)
        rule_obj = self.pool.get('account.statement.completion.rule')

        for call in calls:
            method_to_call = getattr(rule_obj, call)
            result = method_to_call(cr, uid, line, context)
            if result:
                result['already_completed'] = True
                return result
        return None


class AccountStatementCompletionRule(orm.Model):
    """
    This will represent all the completion method that we can have to
    fullfill the bank statement lines. You'll be able to extend them in you own module
    and choose those to apply for every statement profile.
    The goal of a rule is to fullfill at least the partner of the line, but
    if possible also the reference because we'll use it in the reconciliation
    process. The reference should contain the invoice number or the SO number
    or any reference that will be matched by the invoice accounting move.
    """

    _name = "account.statement.completion.rule"
    _order = "sequence asc"

    def _get_functions(self, cr, uid, context=None):
        """
        List of available methods for rules. Override this to add you own.
        """
        return [
            ('get_from_ref_and_invoice', 'From line reference (based on customer invoice number)'),
            ('get_from_ref_and_supplier_invoice', 'From line reference (based on supplier invoice number)'),
            ('get_from_ref_and_so', 'From line reference (based on SO number)'),
            ('get_from_label_and_partner_field', 'From line label (based on partner field)'),
            ('get_from_label_and_partner_name', 'From line label (based on partner name)')]

    _columns = {
        'sequence': fields.integer('Sequence', help="Lower means parsed first."),
        'name': fields.char('Name', size=128),
        'profile_ids': fields.many2many(
            'account.statement.profile',
            rel='as_rul_st_prof_rel',
            string='Related statement profiles'),
        'function_to_call': fields.selection(_get_functions, 'Method'),
    }

    def _find_invoice(self, cr, uid, st_line, inv_type, context=None):
        """Find invoice related to statement line"""
        inv_obj = self.pool.get('account.invoice')
        if inv_type == 'supplier':
            type_domain = ('in_invoice', 'in_refund')
            number_field = 'supplier_invoice_number'
        elif inv_type == 'customer':
            type_domain = ('out_invoice', 'out_refund')
            number_field = 'number'
        else:
            raise osv.except_osv(_('System error'),
                                 _('Invalid invoice type for completion: %') % inv_type)

        inv_id = inv_obj.search(cr, uid,
                                [(number_field, '=', st_line['ref'].strip()),
                                 ('type', 'in', type_domain)],
                                context=context)
        if inv_id:
            if len(inv_id) == 1:
                inv = inv_obj.browse(cr, uid, inv_id[0], context=context)
            else:
                raise ErrorTooManyPartner(_('Line named "%s" (Ref:%s) was matched by more '
                                            'than one partner while looking on %s invoices') %
                                          (st_line['name'], st_line['ref'], inv_type))
            return inv
        return False

    def _from_invoice(self, cr, uid, line, inv_type, context):
        """Populate statement line values"""
        if not inv_type in ('supplier', 'customer'):
            raise osv.except_osv(_('System error'),
                                 _('Invalid invoice type for completion: %') % inv_type)
        res = {}
        inv = self._find_invoice(cr, uid, line, inv_type, context=context)
        if inv:
            res = {'partner_id': inv.partner_id.id,
                   'account_id': inv.account_id.id,
                   'type': inv_type}
            override_acc = line['master_account_id']
            if override_acc:
                res['account_id'] = override_acc
        return res

    # Should be private but data are initialised with no update XML
    def get_from_ref_and_supplier_invoice(self, cr, uid, line, context=None):
        """
        Match the partner based on the invoice supplier invoice number and the reference of the statement
        line. Then, call the generic get_values_for_line method to complete other values.
        If more than one partner matched, raise the ErrorTooManyPartner error.

        :param int/long st_line: read of the concerned account.bank.statement.line
        :return:
            A dict of value that can be passed directly to the write method of
            the statement line or {}
           {'partner_id': value,
            'account_id' : value,

            ...}
        """
        return self._from_invoice(cr, uid, line, 'supplier', context=context)

    # Should be private but data are initialised with no update XML
    def get_from_ref_and_invoice(self, cr, uid, line, context=None):
        """
        Match the partner based on the invoice number and the reference of the statement
        line. Then, call the generic get_values_for_line method to complete other values.
        If more than one partner matched, raise the ErrorTooManyPartner error.

        :param int/long st_line: read of the concerned account.bank.statement.line
        :return:
            A dict of value that can be passed directly to the write method of
            the statement line or {}
           {'partner_id': value,
            'account_id' : value,
            ...}
        """
        return self._from_invoice(cr, uid, line, 'customer', context=context)

    # Should be private but data are initialised with no update XML
    def get_from_ref_and_so(self, cr, uid, st_line, context=None):
        """
        Match the partner based on the SO number and the reference of the statement
        line. Then, call the generic get_values_for_line method to complete other values.
        If more than one partner matched, raise the ErrorTooManyPartner error.

        :param int/long st_line: read of the concerned account.bank.statement.line
        :return:
            A dict of value that can be passed directly to the write method of
            the statement line or {}
           {'partner_id': value,
            'account_id' : value,

            ...}
        """
        st_obj = self.pool.get('account.bank.statement.line')
        res = {}
        if st_line:
            so_obj = self.pool.get('sale.order')
            so_id = so_obj.search(cr,
                                  uid,
                                  [('name', '=', st_line['ref'])],
                                  context=context)
            if so_id:
                if so_id and len(so_id) == 1:
                    so = so_obj.browse(cr, uid, so_id[0], context=context)
                    res['partner_id'] = so.partner_id.id
                elif so_id and len(so_id) > 1:
                    raise ErrorTooManyPartner(_('Line named "%s" (Ref:%s) was matched by more '
                                                'than one partner while looking on SO by ref.') %
                                              (st_line['name'], st_line['ref']))
                st_vals = st_obj.get_values_for_line(cr,
                                                     uid,
                                                     profile_id=st_line['profile_id'],
                                                     master_account_id=st_line['master_account_id'],
                                                     partner_id=res.get('partner_id', False),
                                                     line_type='customer',
                                                     amount=st_line['amount'] if st_line['amount'] else 0.0,
                                                     context=context)
                res.update(st_vals)
        return res

    # Should be private but data are initialised with no update XML
    def get_from_label_and_partner_field(self, cr, uid, st_line, context=None):
        """
        Match the partner based on the label field of the statement line
        and the text defined in the 'bank_statement_label' field of the partner.
        Remember that we can have values separated with ; Then, call the generic
        get_values_for_line method to complete other values.
        If more than one partner matched, raise the ErrorTooManyPartner error.

        :param int/long st_line: read of the concerned account.bank.statement.line
        :return:
            A dict of value that can be passed directly to the write method of
            the statement line or {}
           {'partner_id': value,
            'account_id' : value,

            ...}
            """
        partner_obj = self.pool.get('res.partner')
        st_obj = self.pool.get('account.bank.statement.line')
        res = {}
        # As we have to iterate on each partner for each line,
        # we memoize the pair to avoid
        # to redo computation for each line.
        # Following code can be done by a single SQL query
        # but this option is not really maintanable
        if not context.get('label_memoizer'):
            context['label_memoizer'] = defaultdict(list)
            partner_ids = partner_obj.search(cr,
                                             uid,
                                             [('bank_statement_label', '!=', False)])
            line_ids = context.get('line_ids', [])
            for partner in partner_obj.browse(cr, uid, partner_ids, context=context):
                vals = '|'.join(re.escape(x.strip()) for x in partner.bank_statement_label.split(';'))
                or_regex = ".*%s*." % vals
                sql = ("SELECT id from account_bank_statement_line"
                       " WHERE id in %s"
                       " AND name ~* %s")
                cr.execute(sql, (line_ids, or_regex))
                pairs = cr.fetchall()
                for pair in pairs:
                    context['label_memoizer'][pair[0]].append(partner)
        if st_line['id'] in context['label_memoizer']:
            found_partner = context['label_memoizer'][st_line['id']]
            if len(found_partner) > 1:
                raise ErrorTooManyPartner(_('Line named "%s" (Ref:%s) was matched by '
                                            'more than one partner while looking on partner label') %
                                          (st_line['name'], st_line['ref']))
            res['partner_id'] = found_partner[0].id
            st_vals = st_obj.get_values_for_line(cr,
                                                 uid,
                                                 profile_id=st_line['profile_id'],
                                                 master_account_id=st_line['master_account_id'],
                                                 partner_id=found_partner[0].id,
                                                 line_type=False,
                                                 amount=st_line['amount'] if st_line['amount'] else 0.0,
                                                 context=context)
            res.update(st_vals)
        return res

    def get_from_label_and_partner_name(self, cr, uid, st_line, context=None):
        """
        Match the partner based on the label field of the statement line
        and the name of the partner.
        Then, call the generic get_values_for_line method to complete other values.
        If more than one partner matched, raise the ErrorTooManyPartner error.

        :param int/long st_line: read of the concerned account.bank.statement.line
        :return:
            A dict of value that can be passed directly to the write method of
            the statement line or {}
           {'partner_id': value,
            'account_id' : value,

            ...}
            """
        res = {}
        st_obj = self.pool.get('account.bank.statement.line')
        sql = "SELECT id FROM res_partner WHERE name ~* %s"
        pattern = ".*%s.*" % re.escape(st_line['name'])
        cr.execute(sql, (pattern,))
        result = cr.fetchall()
        if not result:
            return res
        if len(result) > 1:
            raise ErrorTooManyPartner(_('Line named "%s" (Ref:%s) was matched by more '
                                        'than one partner while looking on partner by name') %
                                      (st_line['name'], st_line['ref']))
        res['partner_id'] = result[0][0]
        st_vals = st_obj.get_values_for_line(cr,
                                             uid,
                                             profile_id=st_line['porfile_id'],
                                             master_account_id=st_line['master_account_id'],
                                             partner_id=res['partner_id'],
                                             line_type=False,
                                             amount=st_line['amount'] if st_line['amount'] else 0.0,
                                             context=context)
        res.update(st_vals)
        return res


class AccountStatementLine(orm.Model):
    """
    Add sparse field on the statement line to allow to store all the
    bank infos that are given by a bank/office. You can then add you own in your
    module. The idea here is to store all bank/office infos in the additionnal_bank_fields
    serialized field when importing the file. If many values, add a tab in the bank
    statement line to store your specific one. Have a look in account_statement_base_import
    module to see how we've done it.
    """
    _inherit = "account.bank.statement.line"

    _columns = {
        'additionnal_bank_fields': fields.serialized(
            'Additionnal infos from bank',
            help="Used by completion and import system. Adds every field that "
                 "is present in your bank/office statement file"),
        'label': fields.sparse(
            type='char',
            string='Label',
            serialization_field='additionnal_bank_fields',
            help="Generic field to store a label given from the "
                 "bank/office on which we can base the default/standard "
                 "providen rule."),
        'already_completed': fields.boolean(
            "Auto-Completed",
            help="When this checkbox is ticked, the auto-completion "
                 "process/button will ignore this line."),
    }

    _defaults = {
        'already_completed': False,
    }

    def _get_line_values_from_rules(self, cr, uid, line, rules, context=None):
        """
        We'll try to find out the values related to the line based on rules setted on
        the profile.. We will ignore line for which already_completed is ticked.

        :return:
            A dict of dict value that can be passed directly to the write method of
            the statement line or {}. The first dict has statement line ID as a key:
            {117009: {'partner_id': 100997, 'account_id': 489L}}
        """
        profile_obj = self.pool.get('account.statement.profile')
        res = {}
        errors_stack = []
        if line.get('already_completed'):
            return res
        try:
            # Ask the rule
            vals = profile_obj._find_values_from_rules(cr, uid, rules, line, context)
            if vals:
                vals['id'] = line['id']
                return vals
        except ErrorTooManyPartner as exc:
            msg = "Line ID %s had following error: %s" % (line['id'], exc.value)
            errors_stack.append(msg)
        if errors_stack:
            msg = u"\n".join(errors_stack)
            raise ErrorTooManyPartner(msg)
        return res


class AccountBankSatement(orm.Model):
    """
    We add a basic button and stuff to support the auto-completion
    of the bank statement once line have been imported or manually fullfill.
    """
    _inherit = "account.bank.statement"

    _columns = {
        'completion_logs': fields.text('Completion Log', readonly=True),
    }

    def write_completion_log(self, cr, uid, stat_id, error_msg, number_imported, context=None):
        """
        Write the log in the completion_logs field of the bank statement to let the user
        know what have been done. This is an append mode, so we don't overwrite what
        already recoded.

        :param int/long stat_id: ID of the account.bank.statement
        :param char error_msg: Message to add
        :number_imported int/long: Number of lines that have been completed
        :return : True
        """
        error_log = ""
        user_name = self.pool.get('res.users').read(cr, uid, uid,
                                                    ['name'], context=context)['name']

        log = self.read(cr, uid, stat_id, ['completion_logs'],
                        context=context)['completion_logs']

        log_line = log and log.split("\n") or []
        completion_date = datetime.datetime.now().strftime(DEFAULT_SERVER_DATETIME_FORMAT)
        if error_msg:
            error_log = error_msg
        log_line[0:0] = [completion_date + ' : '
                         + _("Bank Statement ID %s has %s lines completed by %s") %
                         (stat_id, number_imported, user_name)
                         + "\n" + error_log + "-------------" + "\n"]
        log = "\n".join(log_line)
        self.write(cr, uid, [stat_id], {'completion_logs': log}, context=context)

        body = (_('Statement ID %s auto-completed for %s lines completed') %
                (stat_id, number_imported)),
        self.message_post(cr, uid,
                          [stat_id],
                          body=body,
                          context=context)
        return True

    def button_auto_completion(self, cr, uid, ids, context=None):
        """
        Complete line with values given by rules and tic the already_completed
        checkbox so we won't compute them again unless the user untick them !
        """
        if context is None:
            context = {}
        stat_line_obj = self.pool['account.bank.statement.line']
        profile_obj = self.pool.get('account.statement.profile')
        compl_lines = 0
        stat_line_obj.check_access_rule(cr, uid, [], 'create')
        stat_line_obj.check_access_rights(cr, uid, 'create', raise_exception=True)
        for stat in self.browse(cr, uid, ids, context=context):
            msg_lines = []
            ctx = context.copy()
            ctx['line_ids'] = tuple((x.id for x in stat.line_ids))
            b_profile = stat.profile_id
            rules = profile_obj._get_callable(cr, uid, b_profile, context=context)
            profile_id = b_profile.id  # Only for perfo even it gains almost nothing
            master_account_id = b_profile.receivable_account_id
            master_account_id = master_account_id.id if master_account_id else False
            res = False
            for line in stat_line_obj.read(cr, uid, ctx['line_ids']):
                try:
                    # performance trick
                    line['master_account_id'] = master_account_id
                    line['profile_id'] = profile_id
                    res = stat_line_obj._get_line_values_from_rules(cr, uid, line,
                                                                    rules, context=ctx)
                    if res:
                        compl_lines += 1
                except ErrorTooManyPartner, exc:
                    msg_lines.append(repr(exc))
                except Exception, exc:
                    msg_lines.append(repr(exc))
                    error_type, error_value, trbk = sys.exc_info()
                    st = "Error: %s\nDescription: %s\nTraceback:" % (error_type.__name__, error_value)
                    st += ''.join(traceback.format_tb(trbk, 30))
                    print st
                    _logger.error(st)
                if res:
                    #stat_line_obj.write(cr, uid, [line.id], vals, context=ctx)
                    try:
                        stat_line_obj._update_line(cr, uid, res, context=context)
                    except Exception as exc:
                        msg_lines.append(repr(exc))
                        error_type, error_value, trbk = sys.exc_info()
                        st = "Error: %s\nDescription: %s\nTraceback:" % (error_type.__name__, error_value)
                        st += ''.join(traceback.format_tb(trbk, 30))
                        print st
                        _logger.error(st)
                    # we can commit as it is not needed to be atomic
                    # commiting here adds a nice perfo boost
                    if not compl_lines % 500:
                        cr.commit()
            msg = u'\n'.join(msg_lines)
            self.write_completion_log(cr, uid, stat.id,
                                      msg, compl_lines, context=context)
        return True
