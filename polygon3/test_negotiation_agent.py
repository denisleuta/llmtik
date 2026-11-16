"""Проверки извлечения и привязки согласований к актуальной редакции."""
import json
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from polygon3 import negotiation_agent as agent
from polygon3.engine.protocol import TeamEpisode
from polygon3.engine.schema import load_case
from polygon3.runtime.team_sandbox import json_bytes


def sample():
    return load_case(Path(__file__).with_name('cases') / 'case_001.json')


@patch.object(agent.time, 'sleep', new=lambda _delay: None)
class ExtractionTest(unittest.TestCase):
    def test_recovers_case_hyphen_and_named_shape_from_source(self):
        offer = dict(sample().world['offers'][0], id='O-012')
        payload = dict(offer, offer_id='o‑012', warehouse=offer['source'])
        del payload['id'], payload['source']
        docs = [{'text': 'Предложение O-012: артикул S1, склад A1.'}]
        self.assertEqual(agent._wire_facts(json.dumps({'offers': payload}), 'supply', docs), {'offers': [offer]})

    def test_full_record_in_letter_repairs_stock_and_unit_price(self):
        case = sample()
        offer = case.world['offers'][0]
        docs = case.documents['supply']
        damaged = {'offers': [dict(offer, stock=0, unit_price_kopecks=100)]}
        self.assertEqual(agent._wire_facts(json.dumps(damaged), 'supply', docs), {'offers': [offer]})

    def test_nearly_matching_id_is_not_guessed(self):
        offer = dict(sample().world['offers'][0], id='O-12')
        docs = [{'text': 'Предложение O-012: артикул S1, склад A1.'}]
        facts = agent._wire_facts(json.dumps({'offers': [offer]}), 'supply', docs)
        with self.assertRaisesRegex(ValueError, 'O-12'):
            agent._check_sources(facts, 'supply', docs)

    def test_model_uses_source_units_without_rounding(self):
        trip = sample().world['trips'][0]
        payload = dict(trip, departure_min='10:00', arrival_min='11:00',
                       capacity_g='3 кг', fee_kopecks='10 руб.')
        self.assertEqual(agent._wire_facts(json.dumps({'trips': [payload]}), 'logistics'), {'trips': [trip]})
        payload['capacity_g'] = '0,0001 кг'
        with self.assertRaises(ValueError):
            agent._wire_facts(json.dumps({'trips': [payload]}), 'logistics')

    def test_unknown_offer_id_requests_new_extraction(self):
        case = sample()
        doc = case.documents['supply'][0]
        manifest = {doc['id']: doc['revision']}
        offer = dict(case.world['offers'][0], id='O999')
        obs = {'role': 'supply', 'epoch': 1, 'documents': [{k: doc[k] for k in ('id', 'revision')}],
               'last_result': {'type': 'model', 'status': 'ok', 'text': json.dumps({'offers': [offer]})}}
        memory = {'epoch_seen': 1, 'search_round': 0, 'documents': {doc['id']: doc}, 'model_tries': 1,
                  'pending': {'epoch': 1, 'revisions': manifest}}
        memory['documents'][doc['id']] = dict(doc, text='Ответ поставщика.\n\n' + doc['text'])
        action, memory = agent.act(obs, memory)
        self.assertEqual(action['type'], 'model')
        self.assertIn('O999', action['system'])
        self.assertIn('O999', action['messages'][1]['content'])
        self.assertNotIn('facts', memory)

    def test_explicit_offer_missing_from_model_output_is_rejected(self):
        doc = {'text': 'Предложение O-12: товар SKU-7, склад A-1.\nПредложение O-13: товар SKU-7, склад A-1.'}
        facts = {'offers': [{'id': 'O-12', 'sku': 'SKU-7', 'source': 'A-1'}]}
        with self.assertRaisesRegex(ValueError, 'O-13'):
            agent._check_sources(facts, 'supply', [doc])

    def test_sku_from_same_letter_cannot_be_used_as_warehouse(self):
        case = sample()
        offer = case.world['offers'][0]
        with self.assertRaisesRegex(ValueError, offer['source']):
            agent._check_sources({'offers': [dict(offer, source=offer['sku'])]}, 'supply', case.documents['supply'])

    def test_unknown_paragraph_uses_model_instead_of_partial_parse(self):
        doc = sample().documents['supply'][0]
        text = doc['text'].replace('Других предложений нет.', 'Остатки уточните по дополнительному письму.')
        obs = {'role': 'supply', 'epoch': 1, 'documents': [{k: doc[k] for k in ('id', 'revision')}],
               'last_result': {'type': 'read', 'status': 'ok', 'document': dict(doc, text=text)}}
        action, memory = agent.act(obs, {})
        self.assertEqual(action['type'], 'model')
        self.assertNotIn('facts', memory)

    def test_outdated_extraction_cannot_replace_updated_document(self):
        doc = sample().documents['supply'][0]
        first = {'role': 'supply', 'epoch': 1, 'documents': [{k: doc[k] for k in ('id', 'revision')}],
                 'last_result': {'type': 'read', 'status': 'ok', 'document': dict(doc, text='Нестандартное письмо.')}}
        action, memory = agent.act(first, {})
        self.assertEqual(action['type'], 'model')
        newer = dict(first, epoch=2, documents=[{'id': doc['id'], 'revision': 2}],
                     last_result={'type': 'model', 'status': 'ok',
                                  'text': json.dumps({'offers': sample().world['offers']})})
        action, memory = agent.act(newer, memory)
        self.assertEqual(action, {'type': 'read', 'doc_id': doc['id']})
        self.assertNotIn('facts', memory)

    def test_logistics_waits_for_transport_inquiry_before_extraction(self):
        doc = sample().documents['logistics'][0]
        obs = {'role': 'logistics', 'epoch': 1, 'documents': [{k: doc[k] for k in ('id', 'revision')}],
               'last_result': {'type': 'read', 'status': 'ok', 'document': dict(doc, text='Нестандартное письмо.')}}
        action, memory = agent.act(obs, {})
        self.assertEqual(action, {'type': 'wait'})
        request = {'id': 1, 'epoch': 1, 'from': 'supply', 'to': 'logistics',
                   'body': {'kind': 'inquiry', 'epoch': 1}}
        action, _ = agent.act(dict(obs, messages=[request]), memory)
        self.assertEqual(action['type'], 'model')

    def test_compact_response_does_not_accept_bool_as_stock(self):
        offer = sample().world['offers'][0]
        columns = agent.SCHEMAS['supply']['offers'][0]
        valid = {'offers': [[offer[key] for key in columns]]}
        self.assertEqual(agent._wire_facts(json.dumps(valid), 'supply'), {'offers': [offer]})
        valid['offers'][0][list(columns).index('stock')] = True
        with self.assertRaises(ValueError):
            agent._wire_facts(json.dumps(valid), 'supply')


class AgreementTest(unittest.TestCase):
    def test_old_vote_does_not_carry_to_changed_plan(self):
        world = sample().world
        offer, trip = world['offers'][0], world['trips'][0]
        plan = {'status': 'feasible', 'lines': [dict(line_id=line['id'], quantity=line['quantity'],
                                                   offer_id=offer['id'], trip_id=trip['id'])
                                               for line in world['request']['lines']]}
        revisions = {role: {} for role in agent.ROLES}
        proposal = agent.make_proposal(1, 1, world['request'], plan, [offer], [trip], revisions)
        approve = dict(agent.frame_key(proposal), kind='approve', cost_kopecks=0)
        memory = {'proposal': proposal, 'votes': {'supply': approve}}
        self.assertFalse(agent.approvals_current(memory))
        memory['votes']['logistics'] = approve
        self.assertTrue(agent.approvals_current(memory))
        new_trip = dict(trip, fee_kopecks=trip['fee_kopecks'] + 1)
        memory['proposal'] = agent.make_proposal(1, 2, world['request'], plan, [offer], [new_trip], revisions)
        self.assertFalse(agent.approvals_current(memory))


@patch.object(agent.time, 'sleep', new=lambda _delay: None)
class SearchRecoveryTest(unittest.TestCase):
    def run_with_model(self, case_name, damage_first_extraction=False, fail_recheck=False, max_calls=9):
        case = load_case(Path(__file__).with_name('cases') / (case_name + '.json'))
        docs = {role: [dict(doc, text='Уточнение.\n\n' + doc['text']) for doc in values]
                for role, values in case.documents.items()}
        if damage_first_extraction:
            for doc in docs['supply']:
                doc['text'] = doc['text'].replace('На сегодня осталось ', 'В наличии ')
        episode = TeamEpisode(replace(case, documents=docs))
        memories, last = {}, {}
        calls = Counter()
        messages = 0
        for step in range(120):
            role = agent.ROLES[step % 3]
            observation = episode.observation(role)
            observation.update(last_result=last.get(role), budget={
                'model_calls_remaining': max_calls - sum(calls.values()),
                'messages_remaining': 24 - messages, 'actions_remaining': 120 - step})
            action, memory = agent.act(observation, memories.get(role))
            # A later action receives only the memory that passes the real role codec.
            memories[role] = json.loads(json_bytes(memory))
            if action['type'] == 'model':
                self.assertLess(sum(calls.values()), max_calls)
                calls[role] += 1
                key = {'coordinator': 'request', 'supply': 'offers', 'logistics': 'trips'}[role]
                data = episode.debug_case().world[key]
                if damage_first_extraction and role == 'supply' and calls[role] == 1:
                    data = [dict(offer, stock=0) for offer in data]
                reply = 'not valid JSON' if fail_recheck and role == 'supply' and calls[role] > 1 else json.dumps({key: data})
                last[role] = {'type': 'model', 'status': 'ok', 'text': reply}
            else:
                last[role] = episode.apply(role, action)
                messages += action['type'] == 'send'
            episode.advance()
            if episode.finished:
                break
        self.assertTrue(episode.finished)
        self.assertTrue(episode.verdict['correct'], episode.verdict)
        expected = dict.fromkeys(agent.ROLES, 2)
        if damage_first_extraction:
            expected['logistics'] = 1  # Ошибка обнаружена ещё до проверки рейсов.
        if fail_recheck:
            expected['supply'] = 3
        self.assertEqual(calls, Counter(expected))
        return episode.plan

    def test_second_extraction_recovers_a_missed_feasible_plan(self):
        plan = self.run_with_model('case_020', damage_first_extraction=True, max_calls=5)
        self.assertEqual(plan['status'], 'feasible')
        self.assertEqual(plan['lines'][0]['offer_id'], 'O2')

    def test_confirmed_infeasible_still_submits(self):
        plan = self.run_with_model('case_002')
        self.assertEqual(plan, {'status': 'infeasible'})

    def test_failure_during_recheck_preserves_first_grounded_result(self):
        plan = self.run_with_model('case_002', fail_recheck=True)
        self.assertEqual(plan, {'status': 'infeasible'})


if __name__ == '__main__':
    unittest.main()
