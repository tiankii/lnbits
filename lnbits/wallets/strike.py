import asyncio
import json
from typing import AsyncGenerator, Coroutine, Dict
import httpx
from loguru import logger
from lnbits.settings import settings
from lnbits.utils.exchange_rates import fiat_amount_as_satoshis, satoshis_amount_as_fiat
from lnbits.wallets.base import InvoiceResponse, PaymentResponse, PaymentStatus, StatusResponse
from .base import PaymentPendingStatus, Wallet


class StrikeWallet(Wallet):
    """https://docs.strike.me/"""

    def __init__(self):
        self.supported_currencies = {'BTC', 'USD', 'EUR', 'USDT', 'GBP'}

        if not settings.strike_api_endpoint:
            raise ValueError(
                "cannot initialize StrikeWallet: missing strike_api_endpoint")
        if not settings.strike_token:
            raise ValueError(
                "cannot initialize StrikeWallet: missing strike_token")
        if not settings.strike_currency or settings.strike_currency not in self.supported_currencies:
            raise ValueError(
                "cannot initialize StrikeWallet: missing or invalid strike_currency")

        self.endpoint = self.normalize_endpoint(settings.strike_api_endpoint)

        self.auth = {
            "Authorization": f"Bearer {settings.strike_token}",
            "User-Agent": settings.user_agent,
        }
        self.client = httpx.AsyncClient(
            base_url=f"{self.endpoint}/v1", headers=self.auth)

    @property
    def _current_currency(self):
        return settings.strike_currency or 'BTC'

    @property
    def _rate_currency(self):
        return self._current_currency if self._current_currency != 'USDT' else 'USD'

    async def cleanup(self):
        try:
            await self.client.aclose()
        except RuntimeError as e:
            logger.warning(f"Error closing wallet connection: {e}")

    async def status(self) -> StatusResponse:
        try:
            r = await self.client.get("/balances", timeout=8)
            r.raise_for_status()

            data: list = r.json()

            btc_balance = next(
                (
                    wallet["total"]
                    for wallet in data
                    if wallet["currency"] == self._current_currency
                ),
                None,
            )
            if btc_balance is None:
                return StatusResponse("No BTC balance", 0)

            btc_balance = await self._get_btc_amount(float(btc_balance))

            return StatusResponse(None, btc_balance*1000)
        except httpx.HTTPStatusError as err:
            logger.warning(f"HTTP Exception for {err.request.url} - {err.response.json()}")
            return InvoiceResponse(False, None, None, self.get_error_message(err))
        except Exception as exc:
            logger.warning(exc)
            return InvoiceResponse(
                False, None, None, f"Unable to connect to {self.endpoint}."
            )

    async def create_invoice(self, amount: int, memo: str | None = None, description_hash: bytes | None = None, unhashed_description: bytes | None = None, **kwargs) -> InvoiceResponse:
        payload: Dict = {
            "description": memo,
            "amount": {
                "currency": self._current_currency,
                "amount": amount / 100_000_000 if self._current_currency == 'BTC' else await satoshis_amount_as_fiat(amount, self._rate_currency)
            }
        }

        try:
            r = await self.client.post("/invoices", json=payload, timeout=40)
            r.raise_for_status()

            checking_id = r.json()['invoiceId']
            payload2={}

            if description_hash:
                payload2["description_hash"] = description_hash.hex()

            r1 = await self.client.post(f"/invoices/{checking_id}/quote", json=payload2, timeout=40)
            r1.raise_for_status()

            payment_request = r1.json()["lnInvoice"]
            return InvoiceResponse(True, checking_id, payment_request, None)

        except httpx.HTTPStatusError as err:
            logger.warning(f"HTTP Exception for {err.request.url} - {err.response.json()}")
            return InvoiceResponse(False, None, None, self.get_error_message(err))
        except Exception as exc:
            logger.warning(exc)
            return InvoiceResponse(
                False, None, None, f"Unable to connect to {self.endpoint}."
            )

    async def pay_invoice(self, bolt11: str, fee_limit_msat: int) -> PaymentResponse:
        try:
            payload: Dict = {"lnInvoice": bolt11,
                             "sourceCurrency": self._current_currency}

            r = await self.client.post("/payment-quotes/lightning", json=payload, timeout=40)
            r.raise_for_status()

            r1 = await self.client.patch(f"/payment-quotes/{r.json()['paymentQuoteId']}/execute")
            r1.raise_for_status()
            payment = r1.json()

            checking_id = payment["paymentId"]
            fee_msat = (await self._get_btc_amount(float(payment['totalFee']['amount']))) * 1000

            return PaymentResponse(True, checking_id, fee_msat, None, None)

        except httpx.HTTPStatusError as err:
            logger.warning(f"HTTP Exception for {err.request.url} - {err.response.json()}")
            return InvoiceResponse(False, None, None, self.get_error_message(err))
        except Exception as exc:
            logger.warning(exc)
            return InvoiceResponse(
                False, None, None, f"Unable to connect to {self.endpoint}."
            )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        statuses = {
            "UNPAID": None,
            "PENDING": None,
            "PAID": True,
            "CANCELLED": False
        }

        try:
            r = await self.client.get(f"/invoices/{checking_id}")

            if r.is_error:
                return PaymentPendingStatus()

            data = r.json()

            return PaymentStatus(statuses[data["state"]])
        except Exception as e:
            logger.error(f"Error getting invoice status: {e}")
            return PaymentPendingStatus()

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        statuses = {"PENDING": None, "COMPLETED": True, "FAILED": False}

        try:
            r = await self.client.get(f"/payments/{checking_id}")

            if r.is_error:
                return PaymentPendingStatus()

            data = r.json()

            return PaymentStatus(statuses[data.state])
        except Exception as e:
            logger.error(f"Error getting payment status: {e}")
            return PaymentStatus(None)

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        self.queue: asyncio.Queue = asyncio.Queue(0)
        while settings.lnbits_running:
            value = await self.queue.get()
            yield value

    async def _get_btc_amount(self, amount: float):
        if self._rate_currency == "BTC":
            return int(amount * 100_000_000)
        return await fiat_amount_as_satoshis(amount, currency=self._rate_currency)
