"""Guard against lexical substring penalties while retaining the final rubric."""
import unittest

from evaluation.amul.reliability import deterministic_findings
from evaluation.amul.test_final_metrics import turn


class BorrowingInventoryTests(unittest.TestCase):
    def inventory(self, text, glossary=None):
        t=turn(answer_gu=text, glossary=glossary or [])
        return deterministic_findings(t,[t])

    def test_discharge_does_not_count_as_fee_charge(self):
        findings,inventory=self.inventory('સોજો અથવા ડિસ્ચાર્જ છે.')
        self.assertEqual(inventory,[])
        self.assertNotIn('language_purity',findings)

    def test_embedded_compound_does_not_count_as_booking_verb(self):
        self.assertEqual(self.inventory('આ પાસબુકમાં નોંધ છે.')[1],[])

    def test_real_charge_and_inflected_booking_are_still_counted(self):
        findings,inventory=self.inventory('વિઝિટનો ચાર્જ અને બુકિંગ જણાવો.')
        self.assertEqual({x['item'] for x in inventory},{'ચાર્જ','બુક'})
        self.assertEqual(findings['language_purity']['level'],3)

    def test_approved_discharge_does_not_exempt_separate_fee(self):
        findings,inventory=self.inventory('ડિસ્ચાર્જ છે. ચાર્જ કેટલો?', [('discharge','ડિસ્ચાર્જ')])
        self.assertEqual([x['item'] for x in inventory],['ચાર્જ'])
        self.assertEqual(findings['language_purity']['level'],4)

    def test_approved_exact_root_still_exempts_its_inflection(self):
        self.assertEqual(self.inventory('બુકિંગ નોંધાવો.', [('booking','બુકિંગ')])[1],[])

    def test_online_variants_are_one_lexical_item(self):
        findings,inventory=self.inventory('ઓનલાઈન અથવા ઓનલાઇન અરજી કરો.')
        self.assertEqual(len(inventory),1)
        self.assertEqual(findings['language_purity']['level'],4)


if __name__=='__main__':
    unittest.main()
