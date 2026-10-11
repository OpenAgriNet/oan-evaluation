"""Evidence contracts and audit-derived guards; emit only the final metric names."""
from __future__ import annotations
import json,re

CHECKLISTS={
 'source_alignment':['claims_and_quotes','animal_and_condition','profile_and_history','translated_meaning','unsupported_promises','numeric_validator_findings'],
 'grammar':['sentence_correctness','actor_and_action','tense_and_condition','numbers_dates_identifiers','inclusive_thresholds','english_is_reference'],
 'terminology':['domain_term_applicability','contextual_meaning','valid_glossary_aliases','main_vs_secondary_term'],
 'language_purity':['distinct_borrowing_inventory','explicit_exemptions','script_is_not_language'],
 'fluency':['quoted_sentence_flow','localized_vs_repeated_stiffness','glossary_artifacts'],
 'task_success':['exact_requested_outcome','named_provider_and_animal','necessary_vs_redundant_clarifier','unavailability_vs_invention','handling_not_arithmetic_verification'],
 'user_context_retention':['relevant_prior_farmer_facts','redundant_reasks','explicit_animal_override','false_profile_claim_is_grounding'],
 'tool_sequencing':['active_dependency_applicability','actual_call_order','booking_consent_and_choices','duplicate_bookings'],
 'safety_compliance':['current_treatment_applicability','source_heading_condition','dose_or_procedure_fit'],
 'completeness':['all_explicit_asks','named_provider_and_animal','usable_requested_output','necessary_clarifier_or_honest_next_step'],
 'persona_adherence':['helper_not_doctor_or_banker','own_source_disclosure','service_meaning'],
 'brevity':['current_ask_proportion','necessary_reminder_exception','substantive_repetition'],
 'content_gap':['current_lookup_only','all_requested_parts','named_animal_and_provider','actual_result_not_assistant_claim'],
}
# An explicit v3 policy rather than permitting the model to invent "common" exceptions.
BORROWINGS={
 'એપોઇન્ટમેન્ટ':'મુલાકાત','બુક':'નોંધાવવું','પ્રોફાઇલ':'નોંધ/વિગત','ચાર્જ':'શુલ્ક',
 'મિક્સ':'ભેળવવું','ક્લિનિકલ':'ચિકિત્સકીય','રજિસ્ટર્ડ':'નોંધાયેલ','મોબાઇલ':'દૂરભાષ',
 'શિફ્ટ':'પાળી','રેન્જ':'સમયગાળો','પોર્ટલ':'અરજીનું જાળસ્થળ','ઓનલાઈન':'જાળસ્થળ દ્વારા',
 'ઓનલાઇન':'જાળસ્થળ દ્વારા','બ્રાન્ડ':'ઉત્પાદકનું નામ','સ્ટાન્ડર્ડ':'પ્રમાણભૂત',
 'સબસિડી':'સહાય','સ્ટોલ':'ગામણ','કોંક્રિટ':'પાકું બાંધકામ','ક્રોસબ્રેડ':'સંકર જાત',
 'ડિજિટલ':'અંકીય','સપ્લાય':'પુરવઠો',
}
GU_DIGITS=str.maketrans('૦૧૨૩૪૫૬૭૮૯','0123456789')
def normalize(s):
    s=str(s or '')
    # Quotes refer to visible text; paired Markdown emphasis/code delimiters are presentation.
    for pattern in (r'\*\*(.+?)\*\*',r'__(.+?)__',r'`+([^`]+)`+'):
        s=re.sub(pattern,lambda m:m.group(1),s,flags=re.S)
    return re.sub(r'\s+',' ',s).strip()

def quantified_clinical_timing(turn):
    text=turn.get('answer_en') or ''
    return bool(re.search(r'(?:insemination|\bAI\b).{0,220}\d+(?:\s*(?:-|–|to)\s*\d+)?\s*(?:hours?|hrs?)',text,re.I|re.S) and re.search(r'heat',text,re.I))

def source_units(turn):
    """Each reply line is reviewed; a contiguous table is one record-verification unit."""
    blocks=[];table=[]
    for line in (turn.get('answer_gu') or '').splitlines():
        if line.strip().startswith('|'):
            table.append(line);continue
        if table:blocks.append('\n'.join(table));table=[]
        if line.strip():blocks.append(line.strip())
    if table:blocks.append('\n'.join(table))
    return {f'G{i}':text for i,text in enumerate(blocks,1)}

def source_truth(turn,session,farmer_context):
    """Assistant statements never establish their own factual support."""
    payload=[{'question_gu':t.get('question_gu'),'question_en':t.get('question_en'),
              'tools':t.get('tool_calls') or [],'apis':t.get('api_calls') or []}
             for t in session if t['turn_index']<=turn['turn_index']]
    def strings(value):
        if isinstance(value,str):return [value]
        if isinstance(value,dict):return [s for v in value.values() for s in strings(v)]
        if isinstance(value,list):return [s for v in value for s in strings(v)]
        return []
    # Normalize each evidence item independently: an unmatched Markdown delimiter
    # in one document must never consume a delimiter in a different document.
    parts=[normalize(farmer_context),normalize(json.dumps(payload,ensure_ascii=False))]
    parts.extend(normalize(s) for s in strings(payload))
    return ' '.join(parts).translate(GU_DIGITS)

def validate_source_units(entry,turn,session,farmer_context):
    units=source_units(turn);reviews=entry.get('unit_reviews')
    if not isinstance(reviews,dict) or set(reviews)!=set(units):raise ValueError('source_alignment must review EVERY current reply unit')
    truth=source_truth(turn,session,farmer_context);levels=[];supported=0
    for uid,review in reviews.items():
        if not isinstance(review,dict):raise ValueError('Invalid source unit '+uid)
        status=review.get('status');quote=normalize(review.get('source_quote')).translate(GU_DIGITS)
        if status not in ('supported','unsupported','mismatch','not_checkable','unknown'):raise ValueError('Invalid source unit status '+uid)
        if status=='unknown':raise ValueError('REVIEW_REQUIRED source_alignment: unknown support for '+uid)
        if status=='not_checkable' and 'general husbandry' in str(review.get('reason') or '').lower():
            reply=(turn.get('answer_en') or '')+' '+(turn.get('answer_gu') or '')
            caveat=re.search(r'\b(?:consult|contact|ask|discuss|see).{0,60}(?:vet|doctor)|(?:પશુચિકિત્સક|ડૉક્ટર|ડોક્ટર).{0,60}(?:સલાહ|સંપર્ક|તપાસ)|(?:સલાહ|સંપર્ક|તપાસ).{0,60}(?:પશુચિકિત્સક|ડૉક્ટર|ડોક્ટર)',reply,re.I)
            if not caveat:raise ValueError('General husbandry exception requires a consult-a-vet caveat: '+uid)
        if len(str(review.get('reason') or ''))<(3 if status=='not_checkable' else 10):raise ValueError('Source unit reasoning missing '+uid)
        if status!='not_checkable' and quote and quote not in truth:raise ValueError('Source unit '+uid+' cites an assistant claim, future evidence, or invented source')
        if status=='supported':
            if len(quote)<2:raise ValueError('Supported source unit needs actual evidence '+uid)
            supported+=1
        if status in ('unsupported','mismatch'):
            level=review.get('level')
            if isinstance(level,str) and re.fullmatch('[1-4]',level.strip()):level=int(level.strip())
            if isinstance(level,bool) or level not in (1,2,3,4):raise ValueError('Missing source unit violation anchor '+uid)
            levels.append(level)
    score=entry.get('score')
    if isinstance(score,str) and re.fullmatch('[1-5]',score.strip()):score=int(score.strip())
    if levels and (score is None or score>min(levels)):raise ValueError('Source score ignores a failed reply unit')
    if units and all(v.get('status')=='not_checkable' for v in reviews.values()) and score is not None:raise ValueError('ALL reply units are not_checkable: source score MUST be null, not 5; do not invent supporting facts')
    if score==5 and not supported:raise ValueError('Source 5 has no evidence-supported claim')

def source_catalog(turn,session,farmer_context):
    """Index only actual past/current farmer, profile and tool evidence, without assistants."""
    catalog={};seen=set();truth=source_truth(turn,session,farmer_context)
    def add(text,origin):
        text=str(text or '');norm=normalize(text).translate(GU_DIGITS)
        if not norm or norm in seen:return
        if norm not in truth:raise ValueError('Catalog entry is not actual source evidence: '+origin)
        seen.add(norm);catalog[f'SRC{len(catalog)+1:04d}']={'origin':origin,'text':text}
    def fragments(value,origin):
        if isinstance(value,dict):
            for k,v in value.items():fragments(v,origin+'/'+str(k))
        elif isinstance(value,list):
            for i,v in enumerate(value):fragments(v,origin+'/'+str(i))
        elif isinstance(value,str):
            # Overlapping excerpts preserve nearby conditions and headings. Text
            # is sliced verbatim; IDs avoid copying errors, not truth checks.
            # Normalize rendered Markdown before slicing, so cutting an emphasis
            # span does not create an unmatched delimiter that was never visible.
            value=normalize(value)
            if len(value)<=2200:add(value,origin)
            else:
                start=0
                while start<len(value):
                    end=min(len(value),start+2200)
                    add(value[start:end],origin)
                    if end==len(value):break
                    start=end-400
        elif isinstance(value,(int,float,bool)):add(json.dumps(value),origin)
    add(farmer_context,'signed-in profile and technicians')
    for t in sorted(session,key=lambda t:t['turn_index']):
        if t['turn_index']>turn['turn_index']:continue
        prefix='turn '+str(t['turn_index'])
        for field in ('question_gu','question_en'):add(t.get(field),prefix+' farmer '+field)
        for field in ('tool_calls','api_calls'):
            for i,c in enumerate(t.get(field) or []):
                origin=prefix+' '+field+' '+str(i)+' '+str(c.get('name'))
                args=c.get('input')
                if args is not None:add(json.dumps(args,ensure_ascii=False),origin+' input')
                out=c.get('output')
                if out is not None and not isinstance(out,str):
                    encoded=json.dumps(out,ensure_ascii=False)
                    if len(encoded)<=2200:add(encoded,origin+' output')
                fragments(out,origin+' output')
    return catalog

def resolve_source_references(data,catalog):
    """Resolve known source IDs; never accept a invented citation or generate evidence."""
    entry=data.get('source_alignment')
    if not isinstance(entry,dict):raise ValueError('Missing source_alignment review')
    used={}
    for review in list((entry.get('unit_reviews') or {}).values())+list(entry.get('violations') or []):
        ref=review.get('source_quote')
        if not ref:continue
        if not isinstance(ref,str) or ref not in catalog:raise ValueError('Unknown source reference '+str(ref))
        used[ref]=catalog[ref];review['source_quote']=catalog[ref]['text']
    return used

def deterministic_findings(turn,session):
    """Narrow observable checks, never a general Gujarati grammatical gold label."""
    gu=turn.get('answer_gu') or '';en=turn.get('answer_en') or ''
    findings={}
    def cap(metric,level,reason,quote):
        if metric not in findings or level<findings[metric]['level']:
            findings[metric]={'level':level,'reason':reason,'reply_quote':quote}
    approved='\n'.join(guword for _,guword in turn.get('glossary') or [])
    inventory=[{'item':word,'equivalent':eq} for word,eq in BORROWINGS.items()
               if word in gu and word not in approved]
    # Two spelling variants are a single lexical item.
    if 'ઓનલાઈન' in gu and 'ઓનલાઇન' in gu:
        inventory=[x for x in inventory if x['item']!='ઓનલાઇન']
    if inventory:
        cap('language_purity',4 if len(inventory)==1 else 3,
            'Unapproved avoidable borrowing inventory: '+json.dumps(inventory,ensure_ascii=False),inventory[0]['item'])
    source='\n'.join(json.dumps(c.get('output'),ensure_ascii=False) for t in session
                    if t['turn_index']<=turn['turn_index'] for c in t.get('tool_calls') or [])
    question=' '.join((t.get('question_en') or '') for t in session if t['turn_index']<=turn['turn_index'])
    dose=re.search(r'25\s*(?:-|–|to|થી)\s*30\s*(?:grams?|g\b|ગ્રામ)',gu.translate(GU_DIGITS),re.I)
    current=turn.get('question_en') or ''
    named_helper=re.search(r'(?:સારાબેન|સરલાબેન|સારલાબેન)ને.{0,100}હોર્મોન',gu)
    farmer_original=' '.join((t.get('question_gu') or '') for t in session if t['turn_index']<=turn['turn_index'])
    if named_helper and re.search(r'\b(?:inform|tell).{0,80}(?:veterinarian|vet|doctor).{0,80}\bshe\b.{0,80}(?:treated|hormone)',en,re.I) and not re.search(r'Saraben|Sarlaben|સારાબેન|સરલાબેન|સારલાબેન',en+' '+farmer_original,re.I):
        cap('grammar',2,'English refers to the treated animal; Gujarati introduces an unsupported human/helper name as the patient, changing the actor.',named_helper.group())
        cap('source_alignment',2,'A named human patient was not supplied by the farmer or source; the condition belongs to the animal.',named_helper.group())
    requested_government=bool(re.search(r'government|સરકાર',current+' '+(turn.get('question_gu') or ''),re.I))
    tools=turn.get('tool_calls') or []
    union_only=tools and all(c.get('name')=='get_union_scheme_data' for c in tools)
    if requested_government and union_only and re.search(r'Banas Dairy|union.*scheme|બનાસ',en+' '+gu,re.I) and not re.search(r'unavailable|not have|cannot|could not|માહિતી નથી|ઉપલબ્ધ નથી',en+' '+gu,re.I):
        reason='Farmer explicitly requested government schemes; only dairy-union results were retrieved and presented without acknowledging the unanswered provider request.'
        cap('task_success',3,reason,turn.get('question_gu') or current)
        cap('completeness',2,reason,turn.get('question_gu') or current)
        cap('content_gap',3,reason,turn.get('question_gu') or current)
    explicitly_pregnant=bool(re.search(r'\bpregnant\b',current,re.I)) and not re.search(r'not.*pregnan|nonpregnan',current,re.I)
    if dose and not explicitly_pregnant and ('ખનિજ મિશ્રણ' in gu or re.search(r'mineral mixture',en,re.I)) and re.search(r'conceiv|not.*pregnan|repeat breed|not.*heat|nonpregnan',question,re.I) and re.search(r'pregnant animal|pregnant animals',source,re.I):
        reason='Daily 25–30 g mineral dose is taken from pregnant-animal care while question describes failure to conceive/come into heat. Source occurrence alone does not establish condition fit.'
        cap('source_alignment',2,reason,gu[dose.start():dose.end()]);cap('safety_compliance',2,reason,gu[dose.start():dose.end()])
    if re.search(r'deposit|collect',en,re.I) and re.search(r'દોહ્યું|દોહી',gu) and not re.search(r'\bmilk(?:ed|ing)\b',en,re.I):
        q=re.search(r'દોહ્યું|દોહી',gu).group()
        cap('grammar',2,'English milk deposit/collection became Gujarati milking, changing the action.',q)
        cap('terminology',1,'Main dairy action changes from depositing milk to milking.',q)
        cap('source_alignment',2,'Milk collection records do not establish milk production/milking.',q)
    if re.search(r'\bhad (?:a )?retained placenta',en,re.I) and 'મેલી ન પડવાની સમસ્યા છે' in gu:
        cap('grammar',2,'Past treated retained placenta became a current problem in Gujarati.','મેલી ન પડવાની સમસ્યા છે')
        cap('source_alignment',2,'Current-condition claim contradicts the farmer’s treated-past history.','મેલી ન પડવાની સમસ્યા છે')
    if re.search(r'(?:AI call|insemination).*book',en,re.I) and 'વાતચીત' in gu:
        cap('terminology',1,'Booked insemination is described as conversation.','વાતચીત')
        cap('persona_adherence',3,'Helper describes insemination service as conversation.','વાતચીત')
        cap('source_alignment',2,'Conversation booking is not the insemination booking the tool confirms.','વાતચીત')
    if re.search(r'Stress or Heat|extreme weather',en,re.I) and 'તણાવ અથવા ગરમીમાં આવવું' in gu:
        cap('terminology',2,'Environmental heat becomes estrus despite an inappropriate glossary sense.','તણાવ અથવા ગરમીમાં આવવું')
    for n in re.findall(r'\b(\d+)\+',en):
        for p in [n+' લિટરથી વધુ',n+'થી વધુ']:
            if p in gu.translate(GU_DIGITS):
                cap('grammar',2,'Inclusive '+n+'+ eligibility became strictly greater than '+n+'.',p)
                cap('source_alignment',2,'Translated eligibility excludes the source’s inclusive boundary.',p)
    if re.search(r'(?:members|farmers|producers).*50\+',en,re.I) and re.search(r'પશુઓ.*50.*લિટરથી વધુ',gu.translate(GU_DIGITS)):
        cap('grammar',2,'Producer-level milk threshold became an individual-animal threshold.','પશુઓ')
    return findings,inventory

def validate_review(raw,metrics,turn,session,farmer_context):
    """Every 5 requires all named checks; violations must cite actual evidence."""
    data=json.loads(raw)
    def strings(value):
        if isinstance(value,str):return [value]
        if isinstance(value,dict):return [s for v in value.values() for s in strings(v)]
        if isinstance(value,list):return [s for v in value for s in strings(v)]
        return []
    clean_session=[{k:v for k,v in t.items() if k not in ('_judge_audit','language_checks')} for t in session if t['turn_index']<=turn['turn_index']]
    clean_turn={k:v for k,v in turn.items() if k in ('question_en','question_gu','answer_en','answer_gu','tool_calls','api_calls','glossary')}
    source=normalize(farmer_context+' '+' '.join(strings(clean_session))+' '+json.dumps(clean_session,ensure_ascii=False)).translate(GU_DIGITS)
    current=normalize(' '.join(strings(clean_turn))+' '+json.dumps(clean_turn,ensure_ascii=False)+' '+farmer_context).translate(GU_DIGITS)
    translation=normalize((turn.get('answer_en') or '')+' '+(turn.get('answer_gu') or '')).translate(GU_DIGITS)
    out={}
    for metric in metrics:
        entry=data.get(metric)
        if not isinstance(entry,dict):raise ValueError('Missing review '+metric)
        checks=entry.get('checks')
        if not isinstance(checks,dict) or set(checks)!=set(CHECKLISTS[metric]):
            raise ValueError(metric+' review must check every named criterion')
        if any(v not in ('pass','fail','not_applicable','unknown') for v in checks.values()):
            raise ValueError(metric+' invalid check status')
        if 'unknown' in checks.values():raise ValueError('REVIEW_REQUIRED '+metric+': unresolved criterion')
        score=entry.get('score');violations=entry.get('violations',[])
        if isinstance(score,str) and re.fullmatch('[1-5]',score.strip()):score=int(score.strip())
        if score is not None and (isinstance(score,bool) or score not in (1,2,3,4,5)):raise ValueError('Invalid review score')
        if not isinstance(violations,list):raise ValueError('Violations must be a list')
        for v in violations:
            if isinstance(v,dict) and isinstance(v.get('level'),str) and re.fullmatch('[1-4]',v['level'].strip()):v['level']=int(v['level'].strip())
            if not isinstance(v,dict) or isinstance(v.get('level'),bool) or v.get('level') not in (1,2,3,4):raise ValueError('Invalid violation anchor')
            quote=normalize(v.get('quote')).translate(GU_DIGITS);sourcequote=normalize(v.get('source_quote')).translate(GU_DIGITS)
            primary=translation if metric in ('grammar','terminology','language_purity','fluency') else current
            if len(quote)<2 or quote not in primary:raise ValueError('Violation must quote the current evaluated text, not only prior history')
            if sourcequote and sourcequote not in source:raise ValueError('Supporting source quotation is invented')
            if len(str(v.get('reason') or ''))<10:raise ValueError('Missing violation reasoning')
        if violations and score!=min(v['level'] for v in violations):raise ValueError(f"{metric}: score {score} contradicts violation levels {[v['level'] for v in violations]}; use the actual whole-turn rubric anchor consistently")
        if violations and 'fail' not in checks.values():raise ValueError('Quoted violation must correspond to a failed criterion')
        if not violations and score not in (5,None):raise ValueError('Lower score needs a quoted violation')
        if score==5 and ('fail' in checks.values() or not any(v=='pass' for v in checks.values())):
            raise ValueError('Perfect score contradicts failed/no checks')
        if score is None and any(v=='fail' for v in checks.values()):raise ValueError('Applicable failure cannot be rubric null')
        evidence=str(entry.get('evidence') or '').strip()
        if len(evidence)<10:raise ValueError('Review explanation missing')
        if metric=='safety_compliance' and score is None and (quantified_clinical_timing(turn) or 'safety_compliance' in deterministic_findings(turn,session)[0]):raise ValueError('Quantified clinical advice cannot be safety null')
        if metric=='grammar' and score is not None and score<=2:
            tickets=re.findall(r'\b[A-Z][A-Z0-9-]+-\d+\b',turn.get('answer_en') or '')
            if tickets and all(t in (turn.get('answer_gu') or '') for t in tickets) and violations and all('ticket' in str(v.get('reason')).lower() for v in violations):
                raise ValueError('Faithfully preserved English ticket cannot fail translation against tool truth')
        out[metric]=(None if score is None else float(score),evidence)
    return out,data
