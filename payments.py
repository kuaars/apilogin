"""
Оплата. Сейчас работает фейковая платёжка (PAYMENT_PROVIDER=fake, по умолчанию).

Чтобы подключить настоящую:
  1. Создайте класс-наследник PaymentProvider и реализуйте два метода ниже.
  2. Добавьте его в get_provider().
  3. Запустите сервер с PAYMENT_PROVIDER=<ваше имя>.
  4. В кабинете платёжки укажите адрес уведомлений: https://ваш-сайт/payments/webhook

Больше в проекте ничего менять не нужно: заказ становится оплаченным только
через finalize_order() в main.py, а его вызывает вебхук.
"""
import os
import uuid


class PaymentProvider:
    def create_payment(self, order, return_url: str) -> dict:
        """Создаёт платёж в платёжной системе.

        Вернуть: {"payment_id": "<id платежа у провайдера>",
                  "redirect_url": "<куда отправить покупателя для оплаты>"}
        order.id и order.total (целое число, в валюте магазина) уже готовы.
        return_url — куда провайдер вернёт покупателя после оплаты.
        """
        raise NotImplementedError

    def parse_webhook(self, body: bytes, headers: dict) -> dict:
        """Проверяет подпись уведомления от платёжки.

        Вернуть: {"payment_id": "<тот же id, что вернул create_payment>",
                  "success": True или False}
        Если подпись неверная, бросьте ValueError.
        """
        raise NotImplementedError


class FakePaymentProvider(PaymentProvider):
    """Тестовая оплата: покупателя отправляют на нашу страницу с кнопками «Оплатить» / «Отклонить»."""

    def create_payment(self, order, return_url: str) -> dict:
        return {
            "payment_id": f"fake_{uuid.uuid4().hex[:12]}",
            "redirect_url": f"/pay/fake/{order.id}",
        }

    def parse_webhook(self, body: bytes, headers: dict) -> dict:
        raise ValueError("Фейковая оплата не использует вебхуки")


# Пример каркаса настоящей платёжки:
#
# class MyGatewayProvider(PaymentProvider):
#     def create_payment(self, order, return_url):
#         resp = requests.post("https://api.gateway.example/payments", json={
#             "amount": order.total, "order_id": order.id, "return_url": return_url,
#         }, headers={"Authorization": f"Bearer {os.environ['GATEWAY_KEY']}"})
#         data = resp.json()
#         return {"payment_id": data["id"], "redirect_url": data["pay_url"]}
#
#     def parse_webhook(self, body, headers):
#         # 1) сверить подпись из headers с HMAC от body, иначе raise ValueError
#         # 2) разобрать JSON и вернуть payment_id и success
#         ...


def get_provider() -> PaymentProvider:
    name = os.getenv("PAYMENT_PROVIDER", "fake")
    if name == "fake":
        return FakePaymentProvider()
    # if name == "mygateway":
    #     return MyGatewayProvider()
    raise RuntimeError(f"Неизвестная платёжная система: {name}")