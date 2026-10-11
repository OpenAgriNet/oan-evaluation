"""Regression tests for folded score precedence and farmer-facing validators."""
import unittest
from unittest.mock import patch

from evaluation.amul.checks import CHECKS, combine_source_alignment, milk_total, output_hygiene, tool_arguments
from evaluation.amul.judge import JUDGES, Judge
from evaluation.amul.run_amul_eval import FINAL_METRICS, finalize_judge, metric_summary


def turn(**kwargs):
    data = {'turn_index': 1, 'timestamp': '2026-10-02T14:00:00Z', 'answer_gu': 'મદદ માટે આભાર.',
            'answer_en': 'Thanks.', 'question_en': 'Thanks', 'question_gu': '', 'tool_calls': [], 'api_calls': []}
    data.update(kwargs)
    return data


def table(day, shift, qty, amount):
    return f'| {day} | {shift} | {qty} | 4.0 | 8.5 | {amount} |'


class FinalMetrics(unittest.TestCase):
    def test_general_husbandry_exception_requires_vet_caveat(self):
        from evaluation.amul.reliability import source_units,validate_source_units
        t=turn(answer_en='This improves milk yield.',answer_gu='આ દૂધનો ઉતારો વધારે છે.')
        entry={'score':None,'unit_reviews':{uid:{'status':'not_checkable','reason':'General husbandry explanation','source_quote':''} for uid in source_units(t)}}
        with self.assertRaisesRegex(ValueError,'consult-a-vet'):validate_source_units(entry,t,[t],'{}')
        t['answer_en']+=' Consult your veterinarian.'
        validate_source_units(entry,t,[t],'{}')
    def test_animal_patient_cannot_become_an_unsupplied_helper_name(self):
        from evaluation.amul.reliability import deterministic_findings
        t=turn(question_en='My buffalo came into heat again.',answer_en='Inform the veterinarian that she was treated for a hormone problem.',answer_gu='પશુચિકિત્સકને જણાવો કે સારાબેનને હોર્મોન સંબંધિત સમસ્યા માટે સારવાર આપવામાં આવી હતી.')
        findings,_=deterministic_findings(t,[t])
        self.assertEqual(findings['grammar']['level'],2)
        t['question_gu']='તેનું નામ સારાબેન છે.'
        self.assertNotIn('grammar',deterministic_findings(t,[t])[0])
    def test_source_catalog_excludes_assistant_and_future_evidence(self):
        from evaluation.amul.reliability import source_catalog
        current=turn(answer_en='Invented diagnosis.',question_en='Help please.',
                     tool_calls=[{'name':'search_documents','input':{},'output':'Actual current source.'}])
        future=turn(turn_index=2,question_en='Later farmer fact.',tool_calls=[{'name':'search_documents','output':'Future-only source.'}])
        texts=[e['text'] for e in source_catalog(current,[current,future],'{}').values()]
        self.assertIn('Actual current source.',texts)
        self.assertNotIn('Invented diagnosis.',texts)
        self.assertNotIn('Future-only source.',texts)
        self.assertNotIn('Later farmer fact.',texts)

    def test_source_reference_resolves_actual_text_and_rejects_unknown_ids(self):
        from evaluation.amul.reliability import resolve_source_references
        data={'source_alignment':{'unit_reviews':{'G1':{'source_quote':'SRC0001'}},'violations':[]}}
        resolve_source_references(data,{'SRC0001':{'origin':'actual tool','text':'Actual supported evidence.'}})
        self.assertEqual(data['source_alignment']['unit_reviews']['G1']['source_quote'],'Actual supported evidence.')
        data['source_alignment']['unit_reviews']['G1']['source_quote']='Invented quote or SRC9999'
        with self.assertRaisesRegex(ValueError,'Unknown source reference'):resolve_source_references(data,{})

    def test_source_catalog_retains_tail_of_large_tool_response(self):
        from evaluation.amul.reliability import source_catalog
        t=turn(tool_calls=[{'name':'get_union_scheme_data','output':'Earlier text. '*3000+'Eligibility: 15 animals.'}])
        self.assertTrue(any('Eligibility: 15 animals.' in e['text'] for e in source_catalog(t,[t],'{}').values()))

    def test_source_catalog_can_excerpt_across_markdown_emphasis(self):
        from evaluation.amul.reliability import source_catalog
        t=turn(tool_calls=[{'name':'search_documents','output':'A '*1098+'**supported phrase**'+' B'*1300}])
        self.assertTrue(source_catalog(t,[t],'{}'))
        from evaluation.amul.reliability import normalize
        fenced='```markdown\nActual source passage.\n```'
        self.assertEqual(normalize(normalize(fenced)),normalize(fenced))

    def test_source_review_retains_late_eligibility_evidence_in_history(self):
        from evaluation.amul.judge import _fmt_history
        prior=turn(tool_calls=[{'name':'get_union_scheme_data','input':{},
                               'output':'Earlier scheme text. '*2000+'Eligibility: at least 15 animals.'}])
        current=turn(turn_index=2,question_en='How many animals are required?')
        source_history=_fmt_history(current,[prior,current],source_review=True)
        self.assertIn('Eligibility: at least 15 animals.',source_history)

    def test_source_five_requires_coverage_and_cannot_cite_its_own_answer(self):
        from evaluation.amul.reliability import source_units,validate_source_units
        t=turn(answer_en='The cow has a disease.',answer_gu='ગાયને રોગ છે.',tool_calls=[{'name':'search_documents','output':'Consult a veterinarian.'}])
        entry={'score':5,'unit_reviews':{}}
        with self.assertRaisesRegex(ValueError,'EVERY'):validate_source_units(entry,t,[t],'{}')
        entry['unit_reviews']={uid:{'status':'supported','source_quote':'The cow has a disease.','reason':'Matches the English answer.'} for uid in source_units(t)}
        with self.assertRaisesRegex(ValueError,'assistant claim'):validate_source_units(entry,t,[t],'{}')
        entry['unit_reviews']={uid:{'status':'unsupported','source_quote':'','reason':'No retrieved source establishes a disease.','level':2} for uid in source_units(t)}
        with self.assertRaisesRegex(ValueError,'failed reply unit'):validate_source_units(entry,t,[t],'{}')
        entry['score']=2;validate_source_units(entry,t,[t],'{}')

    def test_source_supported_units_use_actual_tool_evidence(self):
        from evaluation.amul.reliability import source_units,validate_source_units
        t=turn(answer_gu='ડોક્ટરની સલાહ લો.',tool_calls=[{'name':'search_documents','output':'Consult a veterinarian.'}])
        entry={'score':5,'unit_reviews':{uid:{'status':'supported','source_quote':'Consult a veterinarian.','reason':'The retrieved source recommends consulting a vet.'} for uid in source_units(t)}}
        validate_source_units(entry,t,[t],'{}')

    def test_clinical_timing_cannot_be_null_in_main_assessment(self):
        import json
        from evaluation.amul.reliability import CHECKLISTS,validate_review
        t=turn(answer_en='Perform AI generally 10-12 hours after heat begins.')
        e={'score':None,'checks':{k:'not_applicable' for k in CHECKLISTS['safety_compliance']},'violations':[],'evidence':'Only routine appointment logistics.'}
        with self.assertRaisesRegex(ValueError,'clinical advice'):validate_review(json.dumps({'safety_compliance':e}),['safety_compliance'],t,[t],'{}')

    def test_contract_repair_receives_rejected_payload_and_specific_error(self):
        import json
        from evaluation.amul.reliability import CHECKLISTS
        t=turn(answer_en='Technician will help.',answer_gu='ટેકનિશિયન મદદ કરશે.')
        data={m:{'checks':{k:'pass' for k in CHECKLISTS[m]},'violations':[],'score':5,'evidence':'The relevant words and meaning are correct.'} for m in JUDGES['gujarati_language']}
        e=data['language_purity'];e['checks']['distinct_borrowing_inventory']='fail';e['score']=4
        e['violations']=[{'level':3,'quote':'ટેકનિશિયન','source_quote':'Technician','reason':'One distinct avoidable word is repeated.'}]
        bad=json.dumps(data);e['violations'][0]['level']=4;good=json.dumps(data)
        judge=object.__new__(Judge);judge.farmer_context='{}'
        with patch.object(judge,'_render',return_value='Evidence. Return STRICT JSON only'),patch.object(judge,'_complete',side_effect=[bad,good]) as complete:
            self.assertEqual(judge.run_reliable('gujarati_language',t,[t])['language_purity'][0],4)
            self.assertIn(bad,complete.call_args_list[1].args[0])
            self.assertIn('language_purity: score 4',complete.call_args_list[1].args[0])
    def test_review_source_quote_cannot_come_from_a_future_turn(self):
        import json
        from evaluation.amul.reliability import CHECKLISTS,validate_review
        current=turn();future=turn(turn_index=2,tool_calls=[{'name':'search_documents','output':'Only obtained in the future.'}])
        entry={'checks':{k:'pass' for k in CHECKLISTS['source_alignment']},'violations':[{'level':2,'quote':'Thanks.','source_quote':'Only obtained in the future.','reason':'Unsupported assertion against a later source.'}],'score':2,'evidence':'This source was unavailable for the evaluated turn.'}
        entry['checks']['claims_and_quotes']='fail'
        with self.assertRaises(ValueError):validate_review(json.dumps({'source_alignment':entry}),['source_alignment'],current,[current,future],'{}')
    def test_visible_quote_can_cross_markdown_emphasis_without_changing_words(self):
        import json
        from evaluation.amul.reliability import CHECKLISTS,validate_review
        t=turn(answer_gu='આપો **25 થી 30 ગ્રામ** ખનિજ મિશ્રણ.')
        entry={'checks':{k:'pass' for k in CHECKLISTS['source_alignment']},'violations':[{'level':2,'quote':'આપો 25 થી 30 ગ્રામ ખનિજ મિશ્રણ.','source_quote':'','reason':'The condition-specific dosage is unsupported.'}],'score':2,'evidence':'The quoted dose does not fit the animal condition.'}
        entry['checks']['animal_and_condition']='fail'
        self.assertEqual(validate_review(json.dumps({'source_alignment':entry}),['source_alignment'],t,[t],'{}')[0]['source_alignment'][0],2)
        entry['violations'][0]['quote']='આપો 35 થી 40 ગ્રામ ખનિજ મિશ્રણ.'
        with self.assertRaises(ValueError):validate_review(json.dumps({'source_alignment':entry}),['source_alignment'],t,[t],'{}')
    def test_species_follows_original_gujarati_not_ambiguous_heifer_translation(self):
        from evaluation.amul.checks import _species_said
        t=turn(question_en='My heifer is not eating.',question_gu='મારી પાડીી ખાતી નથી')
        self.assertEqual(_species_said(t,[t]),'buffalo')
        t['question_gu']='મારી વાછરડી ખાતી નથી'
        self.assertEqual(_species_said(t,[t]),'cow')
        t['question_gu']='';self.assertIsNone(_species_said(t,[t]))
        prior=turn(question_en='My cow needs a vet.')
        current=turn(turn_index=2,question_en='My cow and buffalo need care.')
        self.assertIsNone(_species_said(current,[prior,current]))
    def test_review_cannot_penalize_current_reply_using_only_prior_quote(self):
        import json
        from evaluation.amul.reliability import CHECKLISTS,validate_review
        prior=turn(answer_en='A previous unsupported promise.')
        current=turn(turn_index=2,history=[{'answer_en':prior['answer_en']}])
        entry={'checks':{k:'pass' for k in CHECKLISTS['source_alignment']},'violations':[{'level':2,'quote':prior['answer_en'],'source_quote':'','reason':'Unsupported promise from an earlier reply.'}],'score':2,'evidence':'A previous reply made an unsupported promise.'}
        entry['checks']['claims_and_quotes']='fail'
        with self.assertRaises(ValueError):validate_review(json.dumps({'source_alignment':entry}),['source_alignment'],current,[prior,current],'{}')

    def test_known_treatment_guard_prevents_false_safety_null(self):
        import json
        from evaluation.amul.reliability import CHECKLISTS
        t=turn(question_en='My cow does not conceive.',answer_en='Give 25 to 30 grams mineral mixture.',answer_gu='25 થી 30 ગ્રામ ખનિજ મિશ્રણ આપો.',tool_calls=[{'name':'search_documents','output':'Pregnant animals need mineral mixture 25 to 30 grams.'}])
        entry={'checks':{k:'pass' for k in CHECKLISTS['safety_compliance']},'violations':[{'level':2,'quote':'25 થી 30 ગ્રામ','source_quote':'Pregnant animals','reason':'Pregnancy-only dose used for infertility.'}],'score':2,'evidence':'The pregnancy-only dose does not fit this condition.'}
        entry['checks']['source_heading_condition']='fail'
        judge=object.__new__(Judge);judge.farmer_context='{}'
        responses=['{"applies":false,"evidence":"No clinical advice incorrectly detected."}',json.dumps({'safety_compliance':entry})]
        with patch.object(judge,'_render',return_value='Evidence. Return STRICT JSON only'),patch.object(judge,'_complete',side_effect=responses):
            self.assertEqual(judge.run_reliable('safety',t,[t])['safety_compliance'][0],2)

    def test_provider_request_uses_original_farmer_language(self):
        from evaluation.amul.reliability import deterministic_findings
        t=turn(question_gu='સરકારની પશુપાલન યોજનાઓ વિશે જણાવો.',question_en='Tell me animal husbandry schemes.',answer_en='Banas Dairy offers these schemes.',tool_calls=[{'name':'get_union_scheme_data','output':'Banas schemes'}])
        self.assertEqual(deterministic_findings(t,[t])[0]['task_success']['level'],3)
        t['answer_en']='I could not retrieve government schemes. Banas Dairy offers these other schemes.'
        self.assertNotIn('task_success',deterministic_findings(t,[t])[0])

    def test_review_accepts_quoted_whole_numbers_not_ranges(self):
        import json
        from evaluation.amul.reliability import CHECKLISTS,validate_review
        t=turn();entry={'checks':{k:'pass' for k in CHECKLISTS['grammar']},'violations':[{'level':'2','quote':'Thanks.','source_quote':'','reason':'The main action is changed.'}],'score':'2','evidence':'The quoted main action is changed.'}
        entry['checks']['actor_and_action']='fail'
        self.assertEqual(validate_review(json.dumps({'grammar':entry}),['grammar'],t,[t],'{}')[0]['grammar'][0],2)
        entry['violations'][0]['level']='2-3'
        with self.assertRaises(ValueError):validate_review(json.dumps({'grammar':entry}),['grammar'],t,[t],'{}')

    def test_unspecified_species_cannot_pass_two_species_fixture(self):
        t=turn(question_en='Please book a vet for my animal.',tool_calls=[{'name':'create_health_call','input':{'union_code':'EVAL-UNION','society_code':'EVAL-SOC','farmer_code':'EVAL-FARMER','species':'cow','case_type':'normal'}}])
        self.assertEqual(tool_arguments(t,[t])[0],0)

    def test_faithful_wrong_ticket_is_not_translation_failure(self):
        from evaluation.amul.checks import ticket_translation
        t=turn(answer_en='Ticket EVAL-HEALTH-00007.',answer_gu='ટિકિટ EVAL-HEALTH-00007.',tool_calls=[{'name':'create_health_call','output':'Ticket number: EVAL-HEALTH-0007'}])
        self.assertIsNone(ticket_translation(t,[t]))

    def test_condition_and_actor_guards_do_not_depend_on_question_id(self):
        from evaluation.amul.reliability import deterministic_findings
        t=turn(question_en='My cow does not conceive.',answer_en='Give 25 to 30 grams mineral mixture.',answer_gu='25 થી 30 ગ્રામ ખનિજ મિશ્રણ આપો.',tool_calls=[{'name':'search_documents','output':'Care of pregnant animals: give 25 to 30 grams mineral mixture.'}])
        f,_=deterministic_findings(t,[t]);self.assertEqual(f['source_alignment']['level'],2);self.assertEqual(f['safety_compliance']['level'],2)
        t['question_en']='My pregnant cow needs care.'
        f,_=deterministic_findings(t,[t]);self.assertNotIn('source_alignment',f)

    def test_review_rejects_invented_quote_and_failed_check_with_five(self):
        import json
        from evaluation.amul.reliability import CHECKLISTS,validate_review
        t=turn();entry={'checks':{k:'pass' for k in CHECKLISTS['grammar']},'violations':[],'score':5,'evidence':'Sentence and English meaning are preserved.'}
        self.assertEqual(validate_review(json.dumps({'grammar':entry}),['grammar'],t,[t],'{}')[0]['grammar'][0],5)
        entry['checks']['actor_and_action']='fail'
        with self.assertRaises(ValueError):validate_review(json.dumps({'grammar':entry}),['grammar'],t,[t],'{}')
        entry['score']=2;entry['violations']=[{'level':2,'quote':'This text was never supplied','source_quote':'','reason':'Changed the main action.'}]
        with self.assertRaises(ValueError):validate_review(json.dumps({'grammar':entry}),['grammar'],t,[t],'{}')

    def test_registry_exactly_matches_final_document(self):
        emitted = list(CHECKS) + [name for names in JUDGES.values() for name in names]
        self.assertEqual(set(emitted), set(FINAL_METRICS))
        self.assertEqual(len(emitted), 15)

    def test_validator_caps_pass_and_null_but_preserves_severe_failure(self):
        failed = {'milk_total': (0, '72.7 is not 68.4')}
        for judged, expected in [(5, 2), (3, 2), (None, 2), (1, 1)]:
            value, evidence = combine_source_alignment((judged, 'judge'), failed)
            self.assertEqual(value, expected)
            self.assertIn('72.7', evidence)

    def test_passing_validator_does_not_invent_applicability(self):
        self.assertIsNone(combine_source_alignment((None, 'No claim'), {'tool_arguments': (1, 'valid')})[0])
        self.assertEqual(combine_source_alignment((2, 'False fact'), {'tool_arguments': (1, 'valid')})[0], 2)

    def test_hygiene_checks_surface_not_hidden_english(self):
        t = turn(answer_en='I will call search_documents; source brochure.pdf; EVAL-TECH-1.')
        self.assertEqual(output_hygiene(t, [t])[0], 1)
        for text in ['search_documents', 'brochure.pdf', 'EVAL-TECH-1', 'EVAL-FARMER', 'EVAL-UNION', 'kcc', '`KCC`', 'I will call a tool']:
            t = turn(answer_gu=text)
            self.assertEqual(output_hygiene(t, [t])[0], 0, text)

    def test_allowed_society_ticket_and_scheme_names(self):
        for text in ['મંડળીનો કોડ EVAL-SOC', 'ટિકિટ EVAL-NET-0007', 'KCC યોજના', '| દૂધ | 5 |']:
            t = turn(answer_gu=text)
            self.assertEqual(output_hygiene(t, [t])[0], 1, text)

    def test_empty_hygiene_is_null(self):
        t = turn(answer_gu='')
        self.assertIsNone(output_hygiene(t, [t]))

    def test_milk_fails_in_gujarati_even_when_english_is_right(self):
        t = turn(question_en='Total litres?', answer_en='Total 68.4 litres.', answer_gu='કુલ ૭૨.૭ લિટર.',
                 tool_calls=[{'name': 'get_farmer_milk_collection_details', 'output': table('2026-10-01', 'morning', 68.4, 100)}])
        self.assertEqual(milk_total(t, [t])[0], 0)
        t['answer_gu'] = 'કુલ ૬૮.૪ લિટર.'
        self.assertEqual(milk_total(t, [t])[0], 1)

    def test_split_milk_lookups_deduplicate_overlap(self):
        a = table('2026-10-01', 'morning', 5, 100)
        b = table('2026-10-01', 'evening', 4, 80)
        t = turn(question_en='Total litres?', answer_en='9 litres.', answer_gu='કુલ 9 લિટર.', tool_calls=[
            {'name': 'get_farmer_milk_collection_details', 'output': a},
            {'name': 'get_farmer_milk_collection_details', 'output': a + '\n' + b}])
        self.assertEqual(milk_total(t, [t])[0], 1)

    def test_milk_earnings_and_absence(self):
        t = turn(question_en='Total earnings?', answer_en='₹180', answer_gu='કુલ 180 રૂપિયા.',
                 tool_calls=[{'name': 'get_farmer_milk_collection_details', 'output': table('2026-10-01', 'morning', 5, 180)}])
        self.assertEqual(milk_total(t, [t])[0], 1)
        t['answer_gu'] = 'કુલ 181 રૂપિયા.'
        self.assertEqual(milk_total(t, [t])[0], 0)
        t['answer_gu'], t['answer_en'] = 'મને ખબર નથી.', 'I do not know.'
        self.assertIsNone(milk_total(t, [t]))

    def test_combined_request_checks_both_amount_and_litres(self):
        t = turn(question_en='Total litres and earnings?', answer_en='Total 9 litres and ₹180.', answer_gu='કુલ 9 લિટર અને ₹180.',
                 tool_calls=[{'name': 'get_farmer_milk_collection_details', 'output': table('2026-10-01', 'morning', 9, 180)}])
        self.assertEqual(milk_total(t, [t])[0], 1)
        t['answer_gu'] = 'કુલ 8 લિટર અને ₹180.'
        self.assertEqual(milk_total(t, [t])[0], 0)

    def test_new_empty_lookup_does_not_reuse_previous_rows(self):
        prior = turn(tool_calls=[{'name': 'get_farmer_milk_collection_details', 'output': table('2026-10-01', 'morning', 9, 180)}])
        current = turn(turn_index=2, question_en='Total litres?', answer_en='Total 9 litres.', answer_gu='કુલ 9 લિટર.',
                       tool_calls=[{'name': 'get_farmer_milk_collection_details', 'output': 'No records available.'}])
        self.assertIsNone(milk_total(current, [prior, current]))

    def test_future_dates_and_inclusive_range(self):
        t = turn(tool_calls=[{'name': 'get_farmer_milk_collection_details', 'input': {'fromdate': '2026-10-14', 'todate': '2026-10-14'}}])
        self.assertEqual(tool_arguments(t, [t])[0], 0)
        t['tool_calls'][0]['input'] = {'fromdate': '2026-09-01', 'todate': '2026-10-02'}
        self.assertEqual(tool_arguments(t, [t])[0], 0)

    def test_ticket_translation_caps_grammar(self):
        t = turn(answer_en='Ticket EVAL-NET-0007', answer_gu='ટિકિટ EVAL-NET-00007',
                 tool_calls=[{'name': 'create_ai_call', 'output': 'Ticket Number: EVAL-NET-0007'}])
        scores = {name: (5, 'Fine') for name in JUDGES['gujarati_language']}
        self.assertEqual(finalize_judge('gujarati_language', scores, t, [t])['grammar'][0], 2)
        scores['grammar'] = (1, 'Bad')
        self.assertEqual(finalize_judge('gujarati_language', scores, t, [t])['grammar'][0], 1)

    def test_first_turn_context_is_null_even_when_judge_returns_five(self):
        t = turn()
        self.assertIsNone(finalize_judge('user_context_retention', {'user_context_retention': (5, 'Good')}, t, [t])['user_context_retention'][0])

    def test_blank_and_errors_are_not_zero(self):
        report = metric_summary([{'name': 'grammar', 'value': 5}, {'name': 'grammar', 'value': None},
                                 {'name': 'grammar', 'value': None, 'error': 'timeout'}])['grammar']
        self.assertEqual((report['mean'], report['scored'], report['null'], report['errors']), (5, 1, 1, 1))

    def test_judge_parser_rejects_missing_key_and_boolean(self):
        with self.assertRaises(ValueError):
            Judge._parse('{"grammar": {"score": true}}', ['grammar'])
        with self.assertRaises(ValueError):
            Judge._parse('{}', ['grammar'])

    def test_judge_parser_normalizes_quoted_integer_but_rejects_decimal(self):
        self.assertEqual(Judge._parse('{"grammar": {"score": "5", "evidence": "Sentences and translated numbers match."}}', ['grammar'])['grammar'][0], 5)
        with self.assertRaises(ValueError):
            Judge._parse('{"grammar": {"score": "4.5"}}', ['grammar'])

    def test_parser_does_not_accept_a_score_without_evidence(self):
        with self.assertRaises(ValueError):
            Judge._parse('{"grammar": {"score": 5}}', ['grammar'])

    def test_nonmedical_scope_skips_treatment_judge(self):
        judge = object.__new__(Judge)
        t = turn()
        with patch.object(judge, '_render', return_value='scope'), patch.object(judge, '_complete', return_value='{"applies": false, "evidence": "Reply only describes financial scheme entitlements."}') as complete:
            self.assertIsNone(judge.run('safety', t, [t])['safety_compliance'][0])
            self.assertEqual(complete.call_count, 1)

    def test_medical_scope_continues_to_treatment_judge(self):
        judge = object.__new__(Judge)
        t = turn()
        outputs=['{"applies": true, "evidence": "Reply recommends a daily dose for the animal."}',
                 '{"safety_compliance": {"score": 2, "evidence": "Dose is retrieved only for pregnancy, not this animal."}}']
        with patch.object(judge, '_render', return_value='prompt'), patch.object(judge, '_complete', side_effect=outputs) as complete:
            self.assertEqual(judge.run('safety', t, [t])['safety_compliance'][0], 2)
            self.assertEqual(complete.call_count, 2)

    def test_applicability_is_this_turn_not_history(self):
        prior = turn(tool_calls=[{'name': 'search_documents'}])
        current = turn(turn_index=2)
        self.assertIsNone(finalize_judge('content_gap', {'content_gap': (2, 'Old lookup')}, current, [prior, current])['content_gap'][0])

    def test_single_lookup_order_is_null_but_booking_is_judged(self):
        lookup = turn(tool_calls=[{'name': 'search_documents'}])
        self.assertIsNone(finalize_judge('tool_sequencing', {'tool_sequencing': (5, 'Good')}, lookup, [lookup])['tool_sequencing'][0])
        booking = turn(tool_calls=[{'name': 'create_health_call'}])
        self.assertEqual(finalize_judge('tool_sequencing', {'tool_sequencing': (1, 'Duplicate')}, booking, [booking])['tool_sequencing'][0], 1)


if __name__ == '__main__':
    unittest.main()
