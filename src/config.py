from pathlib import Path
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"

_KEY_INSTRUCTIONS = {
    "MISTRAL_API_KEY": "Obtén tu clave en https://console.mistral.ai/api-keys",
    "SUPABASE_URL": "Obtén la URL en https://supabase.com/dashboard/project/tdyeivstcmtbmzuzrimd/settings/api",
    "SUPABASE_ANON_KEY": "Obtén la anon key en https://supabase.com/dashboard/project/tdyeivstcmtbmzuzrimd/settings/api",
    "TELEGRAM_BOT_TOKEN": "Crea un bot en @BotFather en Telegram y copia el token",
    "TELEGRAM_ADMIN_CHAT_ID": "Envía /start a @userinfobot en Telegram para obtener tu chat_id",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    MISTRAL_API_KEY: str = ""
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ADMIN_CHAT_ID: str = ""
    TELEGRAM_ALLOWED_USERS: str = ""
    CUSTOMER_NIFS: str = ""

    @property
    def customer_nifs_set(self) -> set[str]:
        """NIFs normalizados del restaurante — nunca pertenecen a un proveedor."""
        import re
        return {
            re.sub(r'[^A-Z0-9]', '', n.upper().strip())
            for n in self.CUSTOMER_NIFS.split(",")
            if n.strip()
        }

    @property
    def allowed_users(self) -> list[int]:
        """Lista de user IDs autorizados. Vacía = sin restricción."""
        if not self.TELEGRAM_ALLOWED_USERS.strip():
            return []
        return [int(uid.strip()) for uid in self.TELEGRAM_ALLOWED_USERS.split(",") if uid.strip().isdigit()]

    @model_validator(mode="after")
    def validate_required_keys(self) -> "Settings":
        required = ["MISTRAL_API_KEY", "SUPABASE_URL", "SUPABASE_ANON_KEY", "TELEGRAM_BOT_TOKEN"]
        missing = []
        for key in required:
            value = getattr(self, key, "")
            if not value or not value.strip():
                missing.append(key)

        if missing:
            lines = ["\n❌ Faltan las siguientes variables de entorno en .env:\n"]
            for key in missing:
                instruccion = _KEY_INSTRUCTIONS.get(key, "Consulta la documentación")
                lines.append(f"  • {key}\n    → {instruccion}")
            lines.append(f"\nEdita el archivo: {_ENV_FILE}")
            raise ValueError("\n".join(lines))

        return self


settings = Settings()
