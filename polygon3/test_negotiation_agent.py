"""Проверки извлечения и привязки согласований к актуальной редакции."""
import json
import unittest
from pathlib import Path

from polygon3 import negotiation_agent as agent
from polygon3.engine.schema import load_case


def sample():
    return load_case(Path(__file__).with_name('cases') / 'case_001.json')


class ExtractionTest(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
