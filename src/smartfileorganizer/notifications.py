"""Optional notifications with verified TLS and credential-safe error reporting."""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from typing import Any


class Notifier:
    """Send an execution summary without allowing transports to fail a run.

    Credentials are read at delivery time from the process environment. Neither
    credential values nor exception messages (which can contain token URLs) are
    written to logs.
    """

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config or {}
        self.logger = logger or logging.getLogger("smartfileorganizer")

    @staticmethod
    def _message(summary: dict[str, Any]) -> str:
        counts = summary.get("counts", summary)
        if not isinstance(counts, dict):
            counts = summary

        def count(name: str) -> int:
            try:
                return int(counts.get(name, summary.get(name, 0)))
            except (TypeError, ValueError):
                return 0

        return (
            f"Organización completada: {count('scanned')} archivos examinados, "
            f"{count('moved')} movidos, {count('duplicates')} duplicados, "
            f"{count('skipped')} omitidos y {count('errors')} errores."
        )

    def _timeout(self) -> float:
        timeout = float(self.config.get("timeout_seconds", 10))
        if not 0 < timeout <= 60:
            raise ValueError("El timeout debe estar entre 0 y 60 segundos")
        return timeout

    def send(self, summary: dict[str, Any]) -> None:
        if not self.config.get("enabled", True) or summary.get("mode") == "dry-run":
            return
        channels = self.config.get("channels", [])
        if isinstance(channels, str):
            channels = [channels]
        message = self._message(summary)
        transports = {
            "desktop": self._desktop,
            "email": self._email,
            "telegram": self._telegram,
        }
        for channel in channels:
            transport = transports.get(channel)
            if transport is None:
                self.logger.warning("Canal de notificación desconocido; se omitió")
                continue
            try:
                transport(message)
            except Exception as exc:
                # Do not interpolate exc: transport errors can contain secrets.
                self.logger.warning(
                    "No se pudo enviar la notificación %s (%s)",
                    channel,
                    type(exc).__name__,
                )

    @staticmethod
    def _desktop(message: str) -> None:
        from plyer import notification

        notification.notify(title="SmartFileOrganizer", message=message, timeout=10)

    def _email(self, message: str) -> None:
        required = ("SMTP_HOST", "SMTP_FROM", "SMTP_TO")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            self.logger.warning("Notificación email omitida: faltan %s", ", ".join(missing))
            return
        settings = self.config.get("email", {})
        use_ssl = settings.get("ssl", False)
        port = int(os.environ.get("SMTP_PORT", "465" if use_ssl else "587"))
        if not 1 <= port <= 65535:
            raise ValueError("Puerto SMTP inválido")
        username = os.environ.get("SMTP_USERNAME")
        password = os.environ.get("SMTP_PASSWORD")
        if bool(username) != bool(password):
            self.logger.warning("Notificación email omitida: credenciales SMTP incompletas")
            return
        recipients = [item.strip() for item in os.environ["SMTP_TO"].split(",") if item.strip()]
        if not recipients:
            raise ValueError("Faltan destinatarios SMTP")
        mail = EmailMessage()
        mail["Subject"] = "SmartFileOrganizer: resumen de organización"
        mail["From"] = os.environ["SMTP_FROM"]
        mail["To"] = ", ".join(recipients)
        mail.set_content(message)
        context = ssl.create_default_context()
        options: dict[str, Any] = {"timeout": self._timeout()}
        if use_ssl:
            options["context"] = context
        transport = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with transport(os.environ["SMTP_HOST"], port, **options) as client:
            if not use_ssl:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            if username and password:
                client.login(username, password)
            client.send_message(mail, to_addrs=recipients)

    def _telegram(self, message: str) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            self.logger.warning(
                "Notificación Telegram omitida: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID"
            )
            return
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Formato del token Telegram inválido")
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=self._timeout(), context=ssl.create_default_context()
        ) as response:
            result = json.loads(response.read(65536))
        if not result.get("ok"):
            raise RuntimeError("Telegram rechazó la notificación")
