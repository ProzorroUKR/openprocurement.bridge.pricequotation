# -*- coding: utf-8 -*-
import unittest
from copy import deepcopy
from datetime import datetime

from mock import MagicMock, call, patch
from munch import munchify
from openprocurement.bridge.pricequotation.handlers import PQSecondPhaseCommit, requirement_keys
from openprocurement.bridge.pricequotation.tests.base import TEST_CATEGORY, TEST_PROFILE, TEST_TENDER, AdaptiveCache

from openprocurement_client.exceptions import ResourceGone, ResourceNotFound


class TestPQSecondPhaseCommit(unittest.TestCase):
    config = {
        'worker_config': {
            'handler_priceQuotationBot': {
                'input_resources_api_token': '<secret_api_token>',
                'output_resources_api_token': '<secret_api_token>',
                'resources_api_version': '2.5',
                'input_resources_api_server': 'https://localhost:4443',
                'input_public_resources_api_server': 'https://public.localhost:4443',
                'input_resource': 'tenders',
                'output_resources_api_server': 'https://localhost:4443',
                'output_public_resources_api_server': 'https://public.localhost:4443',
                'output_resource': 'tenders',
                'catalogue_api_server': 'https://e-catalogue.localhost',
                'catalogue_api_version': '0'
             }
        }
    }

    @patch('openprocurement.bridge.pricequotation.handlers.coordination')
    @patch('openprocurement.bridge.pricequotation.handlers.ECataloguesClient')
    @patch('openprocurement.bridge.basic.handlers.APIClient')
    @patch('openprocurement.bridge.pricequotation.handlers.logger')
    def test_init(self, mocked_logger, mocked_catalogues_client, mocked_client, mocked_coordination):
        coordinator = MagicMock()
        mocked_coordination.get_coordinator.return_value = coordinator
        handler = PQSecondPhaseCommit(self.config, 'cache_db')

        self.assertEquals(handler.cache_db, 'cache_db')
        self.assertEquals(handler.handler_config, self.config['worker_config']['handler_priceQuotationBot'])
        self.assertEquals(handler.main_config, self.config)
        self.assertEquals(handler.config_keys,
                          ('input_resources_api_token', 'output_resources_api_token', 'resources_api_version',
                           'input_resources_api_server',
                           'input_public_resources_api_server', 'input_resource', 'output_resources_api_server',
                           'output_public_resources_api_server', 'output_resource')
                          )
        mocked_logger.info.assert_called_once_with('Init priceQuotation second phase commit handler.')
        mocked_coordination.get_coordinator.assert_called_once_with('redis://', 'bridge')
        coordinator.start.assert_called_once_with(start_heart=True)
        self.assertTrue(hasattr(handler, 'coordinator'))
        self.assertTrue(hasattr(handler, 'tender_client'))
        self.assertTrue(hasattr(handler, 'catalogues_client'))


    @patch('openprocurement.bridge.pricequotation.handlers.coordination')
    @patch('openprocurement.bridge.pricequotation.handlers.ECataloguesClient')
    @patch('openprocurement.bridge.pricequotation.handlers.AgreementClient')
    @patch('openprocurement.bridge.basic.handlers.APIClient')
    @patch('openprocurement.bridge.pricequotation.handlers.logger')
    def test_process_resource(self, logger, tender_client, ecatalogue_client, agreement_client, mocked_coordination):
        lock = MagicMock()
        lock._client.exists.return_value = True

        coordinator = MagicMock()
        coordinator.get_lock.return_value = lock

        mocked_coordination.get_coordinator.return_value = coordinator

        cache_db = AdaptiveCache({'0' * 32: datetime.now().isoformat()})
        handler = PQSecondPhaseCommit(self.config, cache_db)

        resource = deepcopy(TEST_TENDER)
        resource['data']['id'] = '0' * 32
        resource = munchify(resource)

        # test lock _client exists
        handler.process_resource(resource.data)
        self.assertEquals(
            logger.info.call_args_list[1:],
            [
                call(
                    'Tender {} processing by another worker.'.format(resource.data.id),
                    extra={'JOURNAL_TENDER_ID': resource.data.id,
                           'MESSAGE_ID': 'tender_already_in_process'}
                )
            ]
        )

        lock._client.exists.return_value = False

        # test not found profile in catalogue
        not_found_exception = ResourceNotFound()
        handler.catalogues_client.profiles.get_profile.side_effect = (not_found_exception,)

        handler.process_resource(resource.data)
        reason = u"Обраний профіль не існує в системі Prozorro.Market"
        self.assertEquals(len(handler.tender_client.patch_resource_item.mock_calls), 1)
        self.assertEquals(
            handler.tender_client.patch_resource_item.mock_calls[0],
            call(resource.data.id, {'data': {'status': 'draft.unsuccessful', 'unsuccessfulReason': [reason]}})
        )
        handler.tender_client.patch_resource_item.reset_mock()

        # test profile not in active status
        _profile = deepcopy(TEST_PROFILE)
        _profile['data']['status'] = 'hidden'
        handler.catalogues_client.profiles.get_profile.side_effect = (munchify(_profile),)

        handler.process_resource(resource.data)
        reason = u"Обраний профіль неактивний в системі Prozorro.Market"
        self.assertEquals(len(handler.tender_client.patch_resource_item.mock_calls), 1)
        self.assertEquals(
            handler.tender_client.patch_resource_item.mock_calls[0],
            call(resource.data.id, {'data': {'status': 'draft.unsuccessful', 'unsuccessfulReason': [reason]}})
        )
        handler.tender_client.patch_resource_item.reset_mock()

        # test with empty list supplier in catalogue category
        handler.catalogues_client.profiles.get_profile.side_effect = (munchify(deepcopy(TEST_PROFILE)),)
        handler.catalogues_client.categories.get_category_suppliers.side_effect = \
            (munchify({'data': [{'status': 'suspended'}]}),)

        # test profile in general status
        _profile = deepcopy(TEST_PROFILE)
        _profile['data']['status'] = 'general'
        handler.catalogues_client.profiles.get_profile.side_effect = (munchify(_profile),)

        handler.process_resource(resource.data)
        reason = u"Обраний профіль (загальний) недоступний для публікації закупівлі \"Запит ціни пропозиції\" в Prozorro.Market"
        self.assertEquals(len(handler.tender_client.patch_resource_item.mock_calls), 1)
        self.assertEquals(
            handler.tender_client.patch_resource_item.mock_calls[0],
            call(resource.data.id, {'data': {'status': 'draft.unsuccessful', 'unsuccessfulReason': [reason]}})
        )
        handler.tender_client.patch_resource_item.reset_mock()

        # test with empty list supplier in catalogue category
        handler.catalogues_client.profiles.get_profile.side_effect = (munchify(deepcopy(TEST_PROFILE)),)
        handler.catalogues_client.categories.get_category_suppliers.side_effect = \
            (munchify({'data': [{'status': 'suspended'}]}),)

        handler.process_resource(resource.data)
        reason = u"В обраному профілі немає активних постачальників"
        self.assertEquals(len(handler.tender_client.patch_resource_item.mock_calls), 1)
        self.assertEquals(
            handler.tender_client.patch_resource_item.mock_calls[0],
            call(resource.data.id, {'data': {'status': 'draft.unsuccessful', 'unsuccessfulReason': [reason]}})
        )
        handler.tender_client.patch_resource_item.reset_mock()

        # test successfull switch to `active.tendering`
        suppliers = {'data': deepcopy(TEST_CATEGORY)['data']['suppliers']}

        handler.catalogues_client.profiles.get_profile.side_effect = (munchify(deepcopy(TEST_PROFILE)),)
        handler.catalogues_client.categories.get_category_suppliers.side_effect = (munchify(suppliers),)

        self.assertNotIn('classification', resource.data['items'][0])
        self.assertNotIn('unit', resource.data['items'][0])
        handler.process_resource(resource.data)

        items = deepcopy(TEST_TENDER['data']['items'])
        items[0]['classification'] = TEST_PROFILE['data']['classification']
        if 'additionalClassifications' in TEST_PROFILE['data']:
            items[0]['additionalClassifications'] = TEST_PROFILE['data']['additionalClassifications']
        items[0]['unit'] = TEST_PROFILE['data']['unit']
        shortlisted_firms = [s for s in TEST_CATEGORY['data']['suppliers'] if s['status'] == 'active']
        criteria = deepcopy(TEST_PROFILE['data']['criteria'])
        for criterion in criteria:
            criterion.pop('code', None)
            for rq_group in criterion['requirementGroups']:
                for rq in rq_group['requirements']:
                    if rq['dataType'] == 'string':
                        continue
                    for key in requirement_keys:
                        if key in rq:
                            rq[key] = str(rq[key])
        patch_data = {
            'data': {
                'criteria': criteria,
                'status': 'active.tendering',
                'items': items,
                'shortlistedFirms': shortlisted_firms
            }
        }
        self.assertEquals(handler.tender_client.patch_resource_item.mock_calls[0], call(resource.data.id, patch_data))


def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(TestPQSecondPhaseCommit))
    return suite


if __name__ == "__main__":
    unittest.main(defaultTest='suite')
