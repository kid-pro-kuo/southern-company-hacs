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

    class CantReachSouthernCompany(Exception):
        pass

    class UsageDataFailure(Exception):
        pass

    class NoJwtTokenFound(Exception):
        pass

    class InvalidLogin(Exception):
        pass

    class EmailValidationRequired(Exception):
        pass

    class NoRequestTokenFound(Exception):
        pass

    class NoScTokenFound(Exception):
        pass

    sca_mock.exceptions.CantReachSouthernCompany = CantReachSouthernCompany
    sca_mock.exceptions.UsageDataFailure = UsageDataFailure
    sca_mock.exceptions.NoJwtTokenFound = NoJwtTokenFound
    sca_mock.exceptions.InvalidLogin = InvalidLogin
    sca_mock.exceptions.EmailValidationRequired = EmailValidationRequired
    sca_mock.exceptions.NoRequestTokenFound = NoRequestTokenFound
    sca_mock.exceptions.NoScTokenFound = NoScTokenFound

    class MockAccount:
        def __init__(self, name, primary, number, company, session):
            self.name = name
            self.primary = primary
            self.number = number
            self.company = company
            self.session = session
            self.service_point_number = None
            self.usage_ids = {}

    class MockMonthlyUsage:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    sca_mock.account.Account = MockAccount
    sca_mock.account.MonthlyUsage = MockMonthlyUsage

    class MockSouthernCompanyAPI:
        def __init__(self, username, password, session):
            self.session = session
            self._jwt = None
            self._jwt_expiry = None
            self._accounts = []

        @property
        async def accounts(self):
            if len(self._accounts) == 0:
                return await self.get_accounts()
            return self._accounts

    sca_mock.parser.SouthernCompanyAPI = MockSouthernCompanyAPI

    sys.modules["southern_company_api"] = sca_mock
    sys.modules["southern_company_api.account"] = sca_mock.account
    sys.modules["southern_company_api.company"] = sca_mock.company
    sys.modules["southern_company_api.constants"] = sca_mock.constants
    sys.modules["southern_company_api.exceptions"] = sca_mock.exceptions
    sys.modules["southern_company_api.parser"] = sca_mock.parser
    sys.modules["southern_company_api.nicor_account"] = sca_mock.nicor_account
    sys.modules["southern_company_api.nicor_parser"] = sca_mock.nicor_parser

import unittest
from unittest.mock import AsyncMock, patch

from custom_components.southern_company.parser_patch import (
    _is_electric,
    _jwt_from_response,
    _select_service_agreement,
    _usage_graph_data,
    _usage_ids,
    company_from,
    deep_find,
    first,
    graph_labels,
    invalidate_usage_ids,
    patched_get_accounts,
    patched_get_jwt,
    patched_get_month_data,
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

    def test_patched_get_jwt_method_resolution(self):
        from custom_components.southern_company.parser_patch import patched_get_jwt

        api_mock = MagicMock()
        api_mock._get_sc_web_token = MagicMock(return_value="mock_web_token")
        del api_mock.get_sc_web_token

        get_token = (
            getattr(api_mock, "_get_sc_web_token", None)
            or getattr(api_mock, "get_sc_web_token", None)
            or getattr(api_mock, "_get_southern_jwt_cookie", None)
        )
        self.assertIsNotNone(get_token)
        self.assertEqual(get_token(), "mock_web_token")


class TestReviewFixes(unittest.IsolatedAsyncioTestCase):
    """Tests covering fixes requested in issue #141 and PR #24 review comments."""

    async def test_get_month_data_requires_essential_totals(self):
        """Review comment 2: Empty graph payload or missing totals must raise UsageDataFailure."""
        from southern_company_api.exceptions import UsageDataFailure

        account = MagicMock()
        account.number = "1234567890"

        with patch(
            "custom_components.southern_company.parser_patch._usage_graph_data",
            new=AsyncMock(return_value={"data": {}}),
        ):
            with self.assertRaises(UsageDataFailure) as ctx:
                await patched_get_month_data(account, "dummy_jwt")
            self.assertIn("Missing essential monthly summary totals", str(ctx.exception))

    async def test_get_month_data_success_with_defaults(self):
        """Review comment 2: When essential totals exist, return MonthlyUsage with optional defaults."""
        account = MagicMock()
        account.number = "1234567890"
        payload = {
            "dollarsToDate": 45.67,
            "totalkWhUsed": 320.5,
            # optional fields omitted
        }

        with patch(
            "custom_components.southern_company.parser_patch._usage_graph_data",
            new=AsyncMock(return_value=payload),
        ):
            result = await patched_get_month_data(account, "dummy_jwt")
            self.assertEqual(result.dollars_to_date, 45.67)
            self.assertEqual(result.total_kwh_used, 320.5)
            self.assertEqual(result.average_daily_cost, 0)
            self.assertEqual(result.projected_bill_amount_high, 0)

    async def test_usage_ids_invalidated_and_refreshed_on_jwt_renewal(self):
        """Review comment 3: Session-scoped IDs must invalidate and refresh when incoming JWT changes."""
        account = MagicMock()
        account.number = "1234"
        account._usage_jwt = "old_jwt"
        account.usage_ids = {"serviceAgreementId": "old_agreement", "servicePointId": "old_sp"}

        async def fake_get_sp(jwt):
            account.usage_ids = {"serviceAgreementId": "new_agreement", "servicePointId": "new_sp"}
            account._usage_jwt = jwt
            return "new_sp"

        account.get_service_point_number = AsyncMock(side_effect=fake_get_sp)

        # Call with new JWT
        ids = await _usage_ids(account, "new_jwt")
        account.get_service_point_number.assert_awaited_once_with("new_jwt")
        self.assertEqual(ids["serviceAgreementId"], "new_agreement")
        self.assertEqual(account._usage_jwt, "new_jwt")

    async def test_usage_graph_data_invalidates_ids_on_auth_failure(self):
        """Review comment 3: Authorization failures (401/403) must invalidate cached usage IDs."""
        from southern_company_api.exceptions import CantReachSouthernCompany

        account = MagicMock()
        account.number = "1234"
        account._usage_jwt = "current_jwt"
        account.usage_ids = {"serviceAgreementId": "agr1", "servicePointId": "sp1"}

        resp_mock = MagicMock()
        resp_mock.status = 401
        resp_cm = AsyncMock()
        resp_cm.__aenter__.return_value = resp_mock
        account.session.get.return_value = resp_cm

        import datetime
        now = datetime.datetime.now()
        with self.assertRaises(CantReachSouthernCompany):
            await _usage_graph_data(
                account,
                "current_jwt",
                "Daily",
                now - datetime.timedelta(days=1),
                now,
            )

        self.assertEqual(account.usage_ids, {})
        self.assertIsNone(account._usage_jwt)

    async def test_patched_get_jwt_invalidates_account_usage_ids_on_token_change(self):
        """Review comment 3: Token change in patched_get_jwt must invalidate cached IDs on known accounts."""
        api = MagicMock()
        api._jwt = "old_token"
        api._get_sc_web_token = AsyncMock(return_value="web_token")
        del api.get_sc_web_token

        acc = MagicMock()
        acc.usage_ids = {"serviceAgreementId": "ag1"}
        acc._usage_jwt = "old_token"
        api._accounts = [acc]

        resp_mock = MagicMock()
        resp_mock.status = 200
        resp_mock.headers = {"ScJwtToken": "new.token.jwt"}
        resp_cm = AsyncMock()
        resp_cm.__aenter__.return_value = resp_mock
        api.session.get.return_value = resp_cm

        new_jwt = await patched_get_jwt(api)
        self.assertEqual(new_jwt, "new.token.jwt")
        self.assertEqual(acc.usage_ids, {})
        self.assertIsNone(acc._usage_jwt)
        self.assertIsNotNone(api._jwt_expiry)

    async def test_patched_get_accounts_resolves_service_points_and_sets_private_attr(self):
        """get_accounts must resolve service point for each account and set _accounts without AttributeError."""
        from southern_company_api.account import Account
        from southern_company_api.parser import SouthernCompanyAPI

        session_mock = MagicMock()
        api = SouthernCompanyAPI("user", "pass", session_mock)
        api._jwt = "valid_jwt"

        resp_mock = MagicMock()
        resp_mock.status = 200
        resp_mock.json = AsyncMock(return_value={
            "data": [
                {
                    "accountNumber": "1001",
                    "description": "Main House",
                    "primaryAccount": "Y",
                    "company": "GPC",
                }
            ]
        })
        resp_cm = AsyncMock()
        resp_cm.__aenter__.return_value = resp_mock
        session_mock.get.return_value = resp_cm

        with patch.object(Account, "get_service_point_number", new=AsyncMock(return_value="SP-1001")):
            accounts = await patched_get_accounts(api)

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].number, "1001")
        # Ensure _accounts was set on the API instance and api.accounts works
        accounts_from_prop = await api.accounts
        self.assertEqual(len(accounts_from_prop), 1)


if __name__ == "__main__":
    unittest.main()
