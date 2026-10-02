"""Monkey-patch southern_company_api for Ascend (OCC) API support and auth fixes.

This patch updates southern_company_api to work with Southern Company's Ascend (OCC)
API estate (occaccountapi and occmypowerusageapi) introduced after the retirement of
customerservice2api, and patches the JWT auth extraction.

Integrates upstream changes from:
- https://github.com/Southern-Company-HA/southern-company-hacs/pull/122
- https://github.com/Southern-Company-HA/southern_company_api/pull/24
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

import aiohttp
from aiohttp import ContentTypeError

import southern_company_api.account
from southern_company_api.account import (
    Account,
    DailyEnergyUsage,
    DailyEnergyUsageList,
    HourlyEnergyUsage,
    MonthlyUsage,
)
from southern_company_api.company import COMPANY_MAP, Company
import southern_company_api.constants as constants
from southern_company_api.exceptions import (
    CantReachSouthernCompany,
    NoJwtTokenFound,
    UsageDataFailure,
)
import southern_company_api.parser as parser
from southern_company_api.parser import SouthernCompanyAPI

_LOGGER = logging.getLogger(__name__)

# --- Update Constants ---
constants.ACCOUNT_API_BASE = "https://occaccountapi.southerncompany.com/api/v1"
constants.MPU_API_BASE = (
    "https://occmypowerusageapi.southerncompany.com/api/v1/MyPowerUsage"
)
constants.GET_ALL_ACCOUNTS_URL = f"{constants.ACCOUNT_API_BASE}/Cap/"
constants.ACCOUNT_SUMMARY_URL = (
    f"{constants.ACCOUNT_API_BASE}/Accounts/{{account}}/Summary"
)
constants.USAGE_GRAPH_DATA_URL = (
    f"{constants.MPU_API_BASE}/UsageGraphData/{{agreement}}/{{granularity}}"
)
constants.BILL_PERIODS_URL = f"{constants.MPU_API_BASE}/BillPeriods"
constants.API_HEADERS["DeviceType"] = "Desktop"

_JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_JWT_COOKIE_RE = re.compile(r"ScJwtToken=(\S*);", re.IGNORECASE)


# --- Helper Functions ---
def first(data: Any, *names: str, default: Any = None) -> Any:
    """First present, non-null value among *names* (case-insensitive)."""
    if not isinstance(data, Mapping):
        return default
    lowered = {str(key).lower(): value for key, value in data.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return default


def company_from(raw: Any, default: Company = Company.GPC) -> Company:
    """Map a company/division code to Company enum."""
    if isinstance(raw, bool) or raw in (None, ""):
        return default
    if isinstance(raw, int):
        return COMPANY_MAP.get(raw, default)
    if isinstance(raw, str):
        token = raw.strip()
        if token.isdigit():
            return COMPANY_MAP.get(int(token), default)
        for company in Company:
            if company.name.lower() == token.lower():
                return company
    return default


def deep_find(payload: Any, key: str, depth: int = 6) -> Any:
    """First value for *key* anywhere in a nested structure, breadth-first."""
    frontier: List[Any] = [payload]
    for _ in range(depth):
        if not frontier:
            return None
        following: List[Any] = []
        for node in frontier:
            if isinstance(node, Mapping):
                value = first(node, key)
                if value not in (None, ""):
                    return value
                following.extend(node.values())
            elif isinstance(node, list):
                following.extend(node)
        frontier = following
    return None


def _series_matches(name: str, wanted: Tuple[str, ...]) -> bool:
    lowered = name.lower()
    if "projected" in lowered:
        return False
    return any(token in lowered for token in wanted)


def series_points(graph: Mapping[str, Any], *wanted: str) -> Dict[str, float]:
    """Collect {label: y} from every graph series matching one of *wanted*."""
    series = graph.get("series") or {}
    if not isinstance(series, Mapping):
        return {}

    matching = [
        (str(name), payload)
        for name, payload in series.items()
        if _series_matches(str(name), wanted)
    ]
    matching.sort(key=lambda item: "delayed" in item[0].lower())

    points: Dict[str, float] = {}
    for name, payload in matching:
        delayed = "delayed" in name.lower()
        for point in (payload or {}).get("data") or []:
            label = point.get("name")
            value = point.get("y")
            if label is None or value is None:
                continue
            if delayed and not value:
                continue
            points.setdefault(label, value)
    return points


def graph_labels(graph: Mapping[str, Any]) -> List[str]:
    """The x-axis labels (ISO timestamps) of a usage graph payload."""
    labels = (graph.get("xAxis") or {}).get("labels")
    return list(labels) if labels else []


def unwrap(response: Any, what: str) -> Any:
    """Unwrap the {statusCode, status, message, data, modelErrors} envelope."""
    payload: Optional[Any] = first(response, "data", "Data")
    if payload is None:
        keys = (
            list(response.keys()) if isinstance(response, Mapping) else type(response)
        )
        raise KeyError(f"No data in {what} response (got {keys})")
    return payload


def _is_electric(agreement: Mapping[str, Any]) -> bool:
    type_code = str(first(agreement, "serviceTypeCode", default="")).strip().lower()
    type_name = str(
        first(
            agreement, "serviceAgreementType", "serviceSubTypeDescription", default=""
        )
    ).lower()
    return type_code == "e" or "electric" in type_name


def _select_service_agreement(
    summary: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    agreements = first(summary, "serviceAgreements") or []
    if not isinstance(agreements, list):
        return None
    usable = [entry for entry in agreements if isinstance(entry, Mapping)]
    active = [entry for entry in usable if first(entry, "isActive") is not False]
    candidates = [entry for entry in active if _is_electric(entry)] or active or usable
    if not candidates:
        return None
    if len(candidates) > 1:
        _LOGGER.warning(
            "Found %d candidate service agreements; using the first (%s).",
            len(candidates),
            first(candidates[0], "serviceSubTypeDescription", "serviceAgreementType"),
        )
    return candidates[0]


def _jwt_from_response(resp: aiohttp.ClientResponse) -> Optional[str]:
    cookies = resp.headers.get("set-cookie")
    if cookies:
        matches = _JWT_COOKIE_RE.search(cookies)
        if matches and matches.group(1):
            return matches.group(1)

    for header in ("ScJwtToken", "ScSoftAuthJwtToken"):
        value = resp.headers.get(header)
        if value and _JWT_RE.fullmatch(value.strip()):
            return value.strip()

    return None


# --- Patched SouthernCompanyAPI Methods ---


async def patched_get_jwt(self: SouthernCompanyAPI) -> str:
    """Get session JWT supporting new header format."""
    get_token = (
        getattr(self, "_get_sc_web_token", None)
        or getattr(self, "get_sc_web_token", None)
        or getattr(self, "_get_southern_jwt_cookie", None)
    )
    if get_token is None:
        raise CantReachSouthernCompany(
            "No method to obtain ScWebToken on SouthernCompanyAPI object"
        )
    sc_web_token = await get_token()
    headers = dict(constants.API_HEADERS)
    headers["ScWebToken"] = sc_web_token
    async with self.session.get(
        constants.JWT_TOKEN_URL,
        headers=headers,
    ) as resp:
        if resp.status != 200:
            raise CantReachSouthernCompany(
                f"Failed to get JWT: {resp.status} {await resp.text()} {headers}"
            )
        token = _jwt_from_response(resp)
        if token is None:
            raise NoJwtTokenFound(
                "Failed to get JWT: no token in set-cookie, in the "
                "ScJwtToken/ScSoftAuthJwtToken response headers, or in the body."
            )
    self._jwt = token
    return token


async def patched_get_accounts(self: SouthernCompanyAPI) -> List[Account]:
    """Get all accounts using OCC API Cap endpoint."""
    if self._jwt is None:
        raise CantReachSouthernCompany(
            f"Can't get jwt. Expired and not refreshed jwt: {self._jwt}"
        )
    headers = dict(constants.API_HEADERS)
    headers["Authorization"] = f"Bearer {self._jwt}"
    async with self.session.get(
        constants.GET_ALL_ACCOUNTS_URL,
        headers=headers,
    ) as resp:
        if resp.status != 200:
            raise CantReachSouthernCompany(
                f"Failed to get accounts: {resp.status} {headers}"
            )
        try:
            account_json = await resp.json()
        except (ContentTypeError, json.JSONDecodeError) as err:
            raise CantReachSouthernCompany(
                f"Incorrect mimetype while trying to get accounts. {resp.headers.get('Content-Type')}"
            ) from err

        accounts = []
        try:
            account_list = account_json.get("data")
            if account_list is None:
                account_list = account_json["Data"]
            for account in account_list:
                number = first(account, "accountNumber", "AccountNumber")
                accounts.append(
                    Account(
                        name=first(
                            account,
                            "description",
                            "Description",
                            "accountName",
                            default=f"Account {number}",
                        ),
                        primary=first(account, "primaryAccount", "PrimaryAccount")
                        in ("Y", "y", True),
                        number=str(number),
                        company=company_from(
                            first(account, "company", "Company", "divisionCode")
                        ),
                        session=self.session,
                    )
                )
        except (KeyError, TypeError) as err:
            raise CantReachSouthernCompany(
                f"Error parsing account JSON payload. {account_json}"
            ) from err
        self.accounts = accounts
        return accounts


# --- Patched Account Methods ---

_orig_account_init = Account.__init__


def patched_account_init(
    self: Account,
    name: str,
    primary: bool,
    number: str,
    company: Company,
    session: aiohttp.ClientSession,
) -> None:
    _orig_account_init(self, name, primary, number, company, session)
    self.usage_ids = getattr(self, "usage_ids", {})


def account_headers(self: Account, jwt: str) -> Dict[str, str]:
    headers = dict(constants.API_HEADERS)
    headers["Authorization"] = f"Bearer {jwt}"
    return headers


async def patched_get_service_point_number(self: Account, jwt: str) -> str:
    """Resolve service point number and usage ids via OCC Account Summary."""
    try:
        async with self.session.get(
            constants.ACCOUNT_SUMMARY_URL.format(account=self.number),
            headers=account_headers(self, jwt),
        ) as resp:
            if resp.status != 200:
                raise CantReachSouthernCompany(
                    f"Failed to get account summary: status {resp.status}"
                )
            try:
                response = await resp.json()
            except (ContentTypeError, json.JSONDecodeError) as err:
                raise CantReachSouthernCompany(
                    f"Incorrect mimetype while trying to get account summary. status:{resp.status}"
                ) from err
    except aiohttp.ClientConnectorError as err:
        raise CantReachSouthernCompany("Failed to connect to api") from err

    try:
        summary = unwrap(response, "account summary")
    except KeyError as err:
        raise CantReachSouthernCompany(str(err)) from err

    agreement = _select_service_agreement(summary)
    if agreement is None:
        _LOGGER.warning(
            "No service agreement for account ending %s; monthly/hourly stats unavailable",
            str(self.number)[-4:],
        )
        self.usage_ids = {}
        self.service_point_number = ""
        return ""

    service_point = deep_find(agreement, "servicePointId")
    if not service_point:
        service_point = deep_find(first(summary, "servicePoints"), "servicePointId")

    ids = {
        "serviceAgreementId": first(agreement, "serviceAgreementId"),
        "servicePointId": service_point,
        "premiseId": first(agreement, "premiseId"),
        "personId": first(summary, "mainPersonId", "personId"),
        "operatingCompany": company_from(
            first(summary, "divisionCode", "operatingCompany"), self.company
        ).name,
    }

    if not ids["serviceAgreementId"] or not ids["servicePointId"]:
        _LOGGER.warning(
            "Could not resolve usage ids for account ending %s; monthly/hourly stats unavailable",
            str(self.number)[-4:],
        )
        self.usage_ids = {}
        self.service_point_number = ""
        return ""

    self.usage_ids = ids
    self.service_point_number = str(ids["servicePointId"])
    return self.service_point_number


async def _usage_ids(self: Account, jwt: str) -> Dict[str, Any]:
    if not getattr(self, "usage_ids", {}).get("serviceAgreementId"):
        await self.get_service_point_number(jwt)
    if not getattr(self, "usage_ids", {}).get("serviceAgreementId"):
        raise UsageDataFailure(
            f"No service agreement for account ending {str(self.number)[-4:]}"
        )
    return self.usage_ids


def _usage_params(
    self: Account,
    ids: Dict[str, Any],
    start_date: datetime.datetime,
    end_date: datetime.datetime,
) -> Dict[str, Any]:
    return {
        "accountId": self.number,
        "personId": ids.get("personId") or "",
        "operatingCompany": ids.get("operatingCompany") or self.company.name,
        "startDate": start_date.strftime("%m/%d/%Y"),
        "endDate": end_date.strftime("%m/%d/%Y"),
        "servicePointId": ids.get("servicePointId") or "",
        "premiseId": ids.get("premiseId") or "",
        "billFactorCode": "null",
    }


async def _usage_graph_data(
    self: Account,
    jwt: str,
    granularity: str,
    start_date: datetime.datetime,
    end_date: datetime.datetime,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Mapping[str, Any]:
    ids = await _usage_ids(self, jwt)
    params = _usage_params(self, ids, start_date, end_date)
    params.update(extra_params or {})
    what = f"{granularity.lower()} data"
    async with self.session.get(
        constants.USAGE_GRAPH_DATA_URL.format(
            agreement=ids["serviceAgreementId"], granularity=granularity
        ),
        headers=account_headers(self, jwt),
        params=params,
    ) as resp:
        if resp.status != 200:
            raise UsageDataFailure(f"Failed to get {what}: {resp.status}")
        try:
            response = await resp.json()
        except (ContentTypeError, json.JSONDecodeError) as err:
            try:
                error_text = await resp.text()
            except aiohttp.ClientError:
                error_text = str(err)
            raise CantReachSouthernCompany(
                f"Incorrect mimetype while trying to get {what}. {error_text}"
            ) from err
    try:
        payload = unwrap(response, what)
    except KeyError as err:
        raise UsageDataFailure(str(err)) from err
    if not isinstance(payload, Mapping):
        raise UsageDataFailure(f"Unexpected {what} payload: {type(payload)}")
    return payload


async def patched_get_daily_data(
    self: Account, start_date: datetime.datetime, end_date: datetime.datetime, jwt: str
) -> List[DailyEnergyUsage]:
    payload = await _usage_graph_data(
        self, jwt, "Daily", start_date, end_date, {"intervalBehavior": "Automatic"}
    )
    days = DailyEnergyUsageList(payload.get("data") or {}).usage()
    self.daily_data = {str(day.date): day for day in days}
    return days


async def patched_get_hourly_data(
    self: Account, start_date: datetime.datetime, end_date: datetime.datetime, jwt: str
) -> List[HourlyEnergyUsage]:
    number_of_chunks = (end_date - start_date).days // 35 + 1
    if number_of_chunks > 1:
        return_data = []
        cur_date = start_date
        for i in range(number_of_chunks):
            window_end = min(cur_date + datetime.timedelta(days=34), end_date)
            try:
                return_data.extend(
                    await self.get_hourly_data(cur_date, window_end, jwt)
                )
            except UsageDataFailure as err:
                _LOGGER.debug(
                    "No hourly data for %s..%s: %s",
                    cur_date.date(),
                    window_end.date(),
                    err,
                )
            cur_date = window_end
            if cur_date >= end_date:
                break
        return return_data

    payload = await _usage_graph_data(
        self, jwt, "Hourly", start_date, end_date, {"intervalBehavior": "Automatic"}
    )
    graph = payload.get("data") or {}
    cost = series_points(graph, "cost")
    usage = series_points(graph, "usage")
    temp = series_points(graph, "temp")

    return_dates = []
    for date in graph_labels(graph):
        parsed_date = datetime.datetime.strptime(date, "%Y-%m-%dT%H:%M:%S")
        parsed_date = parsed_date.replace(
            tzinfo=datetime.timezone(datetime.timedelta(hours=-5), "EST")
        )
        self.hourly_data[date] = HourlyEnergyUsage(
            time=parsed_date,
            usage=usage.get(date),
            cost=cost.get(date),
            temp=temp.get(date),
        )
        return_dates.append(self.hourly_data[date])
    if not return_dates:
        raise UsageDataFailure("Received no data back for usage.")
    return return_dates


async def patched_get_month_data(self: Account, jwt: str) -> MonthlyUsage:
    today = datetime.datetime.now()
    first_of_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    payload = await _usage_graph_data(
        self, jwt, "Daily", first_of_month, today, {"intervalBehavior": "Automatic"}
    )
    return MonthlyUsage(
        dollars_to_date=first(payload, "dollarsToDate", default=0),
        total_kwh_used=first(payload, "totalkWhUsed", default=0),
        average_daily_usage=first(payload, "averageDailyUsage", default=0),
        average_daily_cost=first(payload, "averageDailyCost", default=0),
        projected_usage_low=first(payload, "projectedUsageLow", default=0),
        projected_usage_high=first(payload, "projectedUsageHigh", default=0),
        projected_bill_amount_low=first(
            payload, "projectedBillAmountLow", default=0
        ),
        projected_bill_amount_high=first(
            payload, "projectedBillAmountHigh", default=0
        ),
    )


# --- DailyEnergyUsageList patch ---


def patched_daily_usage_list_usage(
    self: DailyEnergyUsageList,
) -> List[DailyEnergyUsage]:
    cost = series_points(self.data, "cost")
    usage = series_points(self.data, "usage")
    high_temps = series_points(self.data, "hightemp")
    low_temps = series_points(self.data, "lowtemp")

    days = [
        DailyEnergyUsage(
            date=datetime.datetime.strptime(date, "%Y-%m-%dT%H:%M:%S"),
            usage=usage.get(date),
            cost=cost.get(date),
            low_temp=low_temps.get(date),
            high_temp=high_temps.get(date),
        )
        for date in graph_labels(self.data)
    ]
    return days


# --- Apply Patches Function ---

_patched = False


def apply_patches() -> None:
    """Apply all monkey-patches to southern_company_api."""
    global _patched
    if _patched:
        return

    try:
        SouthernCompanyAPI.get_jwt = patched_get_jwt
        SouthernCompanyAPI.get_accounts = patched_get_accounts

        if isinstance(Account, type):
            Account.__init__ = patched_account_init
        Account.get_service_point_number = patched_get_service_point_number
        Account.get_daily_data = patched_get_daily_data
        Account.get_hourly_data = patched_get_hourly_data
        Account.get_month_data = patched_get_month_data

        if isinstance(DailyEnergyUsageList, type):
            DailyEnergyUsageList.usage = patched_daily_usage_list_usage
    except Exception as err:
        _LOGGER.warning(
            "Could not apply some patches to southern_company_api: %s", err
        )

    _patched = True


apply_patches()
