"""Unit tests for parser_patch.py OCC API monkeypatching and helpers."""

import sys
from unittest.mock import MagicMock

# Mock homeassistant, aiohttp, and southern_company_api if not installed in environment
for ha_mod in [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.recorder",
    "homeassistant.components.recorder.models",
    "homeassistant.components.recorder.statistics",
    "homeassistant.components.sensor",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.selector",
    "homeassistant.helpers.update_coordinator",
    "voluptuous",
]:
    if ha_mod not in sys.modules:
        sys.modules[ha_mod] = MagicMock()

if "aiohttp" not in sys.modules:
    aiohttp_mock = MagicMock()
    aiohttp_mock.ContentTypeError = Exception
    sys.modules["aiohttp"] = aiohttp_mock

if "southern_company_api" not in sys.modules:
    sca_mock = MagicMock()
    sca_mock.company.COMPANY_MAP = {1: MagicMock(name="GPC")}
    sca_mock.company.Company = MagicMock()
    sca_mock.company.Company.GPC = MagicMock(name="GPC")
    sca_mock.company.Company.APC = MagicMock(name="APC")
    sca_mock.company.Company.MPC = MagicMock(name="MPC")
    sca_mock.constants.API_HEADERS = {}
    sys.modules["southern_company_api"] = sca_mock
    sys.modules["southern_company_api.account"] = sca_mock.account
    sys.modules["southern_company_api.company"] = sca_mock.company
    sys.modules["southern_company_api.constants"] = sca_mock.constants
    sys.modules["southern_company_api.exceptions"] = sca_mock.exceptions
    sys.modules["southern_company_api.parser"] = sca_mock.parser
    sys.modules["southern_company_api.nicor_account"] = sca_mock.nicor_account
    sys.modules["southern_company_api.nicor_parser"] = sca_mock.nicor_parser

import unittest

from custom_components.southern_company.parser_patch import (
    _is_electric,
    _jwt_from_response,
    _select_service_agreement,
    company_from,
    deep_find,
    first,
    graph_labels,
    series_points,
    unwrap,
)


class TestParserPatchHelpers(unittest.TestCase):
    def test_first(self):
        data = {"CamelCase": "val1", "lower": "val2"}
        self.assertEqual(first(data, "camelcase"), "val1")
        self.assertEqual(first(data, "LOWER"), "val2")
        self.assertEqual(first(data, "nonexistent", default="def"), "def")

    def test_company_from(self):
        self.assertIsNotNone(company_from(1))
        self.assertIsNotNone(company_from("GPC"))

    def test_deep_find(self):
        payload = {
            "level1": {
                "level2": [
                    {"targetKey": "found_me"}
                ]
            }
        }
        self.assertEqual(deep_find(payload, "targetKey"), "found_me")
        self.assertIsNone(deep_find(payload, "missingKey"))

    def test_series_points_plain_beats_delayed(self):
        graph = {
            "series": {
                "usageDelayed": {"data": [{"name": "2026-09-01T00:00:00", "y": 9.9}]},
                "usage": {"data": [{"name": "2026-09-01T00:00:00", "y": 1.5}]},
            }
        }
        self.assertEqual(series_points(graph, "usage"), {"2026-09-01T00:00:00": 1.5})

    def test_series_points_delayed_zero_ignored(self):
        graph = {
            "series": {
                "usage": {"data": [{"name": "2026-09-01T00:00:00", "y": 1.5}]},
                "usageDelayed": {"data": [{"name": "2026-09-01T01:00:00", "y": 0}]},
            }
        }
        self.assertEqual(series_points(graph, "usage"), {"2026-09-01T00:00:00": 1.5})

    def test_graph_labels(self):
        graph = {"xAxis": {"labels": ["2026-09-01T00:00:00", "2026-09-01T01:00:00"]}}
        self.assertEqual(
            graph_labels(graph), ["2026-09-01T00:00:00", "2026-09-01T01:00:00"]
        )

    def test_unwrap(self):
        resp_data = {"statusCode": 200, "data": {"key": "val"}}
        self.assertEqual(unwrap(resp_data, "test"), {"key": "val"})
        with self.assertRaises(KeyError):
            unwrap({}, "test")

    def test_is_electric(self):
        self.assertTrue(_is_electric({"serviceTypeCode": "E"}))
        self.assertTrue(_is_electric({"serviceAgreementType": "Electric Residential"}))
        self.assertFalse(_is_electric({"serviceTypeCode": "L", "serviceAgreementType": "Lighting"}))

    def test_select_service_agreement(self):
        summary = {
            "serviceAgreements": [
                {
                    "serviceAgreementId": "lighting-1",
                    "serviceTypeCode": "L",
                    "isActive": True,
                },
                {
                    "serviceAgreementId": "electric-1",
                    "serviceTypeCode": "E",
                    "isActive": True,
                },
            ]
        }
        ag = _select_service_agreement(summary)
        self.assertIsNotNone(ag)
        self.assertEqual(ag["serviceAgreementId"], "electric-1")

    def test_jwt_from_response(self):
        resp_header = MagicMock()
        resp_header.headers = {
            "set-cookie": "ScJwtToken=header_cookie_jwt_token_val; path=/",
        }
        token = _jwt_from_response(resp_header)
        self.assertEqual(token, "header_cookie_jwt_token_val")

        resp_bare_header = MagicMock()
        valid_jwt = "header.payload.signature"
        resp_bare_header.headers = {
            "ScJwtToken": valid_jwt,
        }
        token2 = _jwt_from_response(resp_bare_header)
        self.assertEqual(token2, valid_jwt)


if __name__ == "__main__":
    unittest.main()
