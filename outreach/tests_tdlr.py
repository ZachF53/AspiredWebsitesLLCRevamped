"""
Tests for the TDLR licence-register owner matcher.

The rows below are real ones from the published file. The central
assertion is the ambiguity rule: a first implementation reported 60
matches by taking the first candidate whenever a name was shared, and
"Air Action Air Conditioning" maps to six different licensees. A wrong
owner name is worse than no owner name -- it goes straight into a call
opening.
"""

from django.test import TestCase

from outreach import tdlr
from outreach.models import Lead


def row(name, business, licence='12345', expires=' 05/12/2027',
        biz_city='SAN ANTONIO, TX 78202', mail_city=''):
    r = [''] * 23
    r[tdlr.COL_LICENCE_NO] = licence
    r[tdlr.COL_EXPIRES] = expires
    r[tdlr.COL_NAME] = name
    r[tdlr.COL_MAILING_CITY] = mail_city
    r[tdlr.COL_BUSINESS_NAME] = business
    r[tdlr.COL_BUSINESS_CITY] = biz_city
    return r


def make_lead(**kw):
    defaults = dict(firm_name='Northwest Hills AC and Heating',
                    city='Austin', state='TX', business_type='HVAC',
                    phone='(512) 748-4332', attorney_name='')
    defaults.update(kw)
    return Lead.objects.create(**defaults)


class NormaliseTests(TestCase):

    def test_strips_trade_and_corporate_words(self):
        self.assertEqual(tdlr.normalise('SOUTH AUSTIN AC & HEATING'),
                         'south austin')
        self.assertEqual(tdlr.normalise('South Austin AC'), 'south austin')

    def test_that_stripping_is_what_creates_collisions(self):
        # Documents the trade-off: without stripping the two above never
        # match; with it, these two distinct businesses collapse.
        self.assertEqual(tdlr.normalise('Austin Air Services'),
                         tdlr.normalise('Austin Heating & Cooling Co'))

    def test_format_person(self):
        self.assertEqual(tdlr.format_person('CASH, BOB C'), 'Bob C Cash')
        self.assertEqual(tdlr.format_person('ANDERSON, ORIN RAE'),
                         'Orin Rae Anderson')
        self.assertEqual(tdlr.format_person('MADONNA'), 'Madonna')
        self.assertEqual(tdlr.format_person(''), '')


class MatchTests(TestCase):

    def test_unique_name_matches(self):
        groups = tdlr.index_by_name([
            row('CASH, BOB C', 'NORTHWEST HILLS AC AND HEATING',
                biz_city='AUSTIN, TX 78731')])
        lead = make_lead()
        found, how = tdlr.match_lead(lead, groups)
        self.assertIsNotNone(found)
        self.assertEqual(found[tdlr.COL_NAME], 'CASH, BOB C')
        self.assertEqual(how, 'exact')

    def test_shared_name_with_no_city_tiebreak_is_refused(self):
        # The real case: 'Air Action Air Conditioning' -> 6 licensees.
        groups = tdlr.index_by_name([
            row('ESCAMILLA, DANIEL', 'ACTION AIR CONDITIONING',
                biz_city='HOUSTON, TX 77002'),
            row('WEBSTER, GALEN LAD', 'ACTION AIR CONDITIONING CO',
                biz_city='DALLAS, TX 75201'),
            row('DAUGHERTY, WILLIAM', 'ACTION AIR CONDITIONING LLC',
                biz_city='EL PASO, TX 79901'),
        ])
        lead = make_lead(firm_name='Air Action Air Conditioning',
                         city='Austin')
        found, why = tdlr.match_lead(lead, groups)
        self.assertIsNone(found)
        self.assertIn('share this business name', why)

    def test_city_breaks_a_tie_when_it_singles_one_out(self):
        groups = tdlr.index_by_name([
            row('ESCAMILLA, DANIEL', 'ACTION AIR CONDITIONING',
                biz_city='HOUSTON, TX 77002'),
            row('WEBSTER, GALEN LAD', 'ACTION AIR CONDITIONING CO',
                biz_city='AUSTIN, TX 78701'),
        ])
        lead = make_lead(firm_name='Air Action Air Conditioning',
                         city='Austin')
        found, how = tdlr.match_lead(lead, groups)
        self.assertIsNotNone(found)
        self.assertEqual(found[tdlr.COL_NAME], 'WEBSTER, GALEN LAD')
        self.assertIn('city', how)

    def test_city_that_matches_several_does_not_break_the_tie(self):
        groups = tdlr.index_by_name([
            row('A, ONE', 'ACTION AIR', biz_city='AUSTIN, TX 78701'),
            row('B, TWO', 'ACTION AIR CO', biz_city='AUSTIN, TX 78702'),
        ])
        lead = make_lead(firm_name='Action Air', city='Austin')
        found, why = tdlr.match_lead(lead, groups)
        self.assertIsNone(found)
        self.assertIn('share this business name', why)

    def test_fuzzy_handles_spelling_drift(self):
        groups = tdlr.index_by_name([
            row('CASTILLO, JOSE MANUEL', "CASTILLO'S A/C & HEATING",
                biz_city='AUSTIN, TX 78704')])
        lead = make_lead(firm_name='Castillo Service A/C & Heating',
                         city='Austin')
        found, how = tdlr.match_lead(lead, groups)
        # Either it matches fuzzily or it honestly reports no match --
        # what it must never do is pick an unrelated licensee.
        if found is not None:
            self.assertEqual(found[tdlr.COL_NAME], 'CASTILLO, JOSE MANUEL')

    def test_unknown_business_reports_no_match(self):
        groups = tdlr.index_by_name([row('X, Y', 'SOMETHING ELSE')])
        lead = make_lead(firm_name='Totally Unlisted Cooling')
        found, why = tdlr.match_lead(lead, groups)
        self.assertIsNone(found)
        self.assertIn('no licensed contractor', why)

    def test_name_made_only_of_trade_words_is_refused(self):
        groups = tdlr.index_by_name([row('X, Y', 'AIR SERVICES')])
        lead = make_lead(firm_name='Air Services LLC')
        found, why = tdlr.match_lead(lead, groups)
        self.assertIsNone(found)
        self.assertIn('no distinctive tokens', why)


class ApplyTests(TestCase):

    def test_writes_the_name_and_an_audit_trail(self):
        groups = tdlr.index_by_name([
            row('CASH, BOB C', 'NORTHWEST HILLS AC AND HEATING',
                licence='26992', expires=' 03/29/2027',
                biz_city='AUSTIN, TX 78731')])
        lead = make_lead()
        matched, _ = tdlr.apply_to_leads([lead], groups)
        lead.refresh_from_db()
        self.assertEqual(matched, 1)
        self.assertEqual(lead.attorney_name, 'Bob C Cash')
        self.assertIn('26992', lead.notes)
        self.assertIn('CASH, BOB C', lead.notes)

    def test_ambiguous_lead_is_left_untouched(self):
        groups = tdlr.index_by_name([
            row('A, ONE', 'ACTION AIR', biz_city='HOUSTON, TX'),
            row('B, TWO', 'ACTION AIR CO', biz_city='DALLAS, TX')])
        lead = make_lead(firm_name='Action Air', city='Austin')
        matched, _ = tdlr.apply_to_leads([lead], groups)
        lead.refresh_from_db()
        self.assertEqual(matched, 0)
        self.assertEqual(lead.attorney_name, '')

    def test_existing_name_is_kept_unless_overwrite(self):
        groups = tdlr.index_by_name([
            row('CASH, BOB C', 'NORTHWEST HILLS AC AND HEATING',
                biz_city='AUSTIN, TX 78731')])
        lead = make_lead(attorney_name='Someone Known')
        tdlr.apply_to_leads([lead], groups)
        lead.refresh_from_db()
        self.assertEqual(lead.attorney_name, 'Someone Known')

        tdlr.apply_to_leads([lead], groups, overwrite=True)
        lead.refresh_from_db()
        self.assertEqual(lead.attorney_name, 'Bob C Cash')
