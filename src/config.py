from pathlib import Path
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"

_KEY_INSTRUCTIONS = {
    "MISTRAL_API_KEY": "Obtén tu clave en https://console.mistral.ai/api-keys",
    "SUPABASE_URL": "Obtén la URL en https://supabase.com/dashboard/project/tdyeivstcmtbmzuzrimd/settings/api",
    "SUPABASE_ANON_KEY": "Obtén la anon key en https://supabase.com/dashboard/project/tdyeivstcmtbmzuzrimd/settings/api",
    "SUPABASE_SERVICE_ROLE_KEY": "Obtén la service role en el panel de Supabase y guárdala solo en el backend",
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
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ADMIN_CHAT_ID: str = ""
    TELEGRAM_ALLOWED_USERS: str = ""
    CUSTOMER_NIFS: str = ""
    ENVIRONMENT: str = "development"
    STORAGE_BUCKET: str = "albaranes"
    MAX_DOCUMENT_BYTES: int = 15 * 1024 * 1024
    MAX_PENDING_PER_USER: int = 20
    MAX_PENDING_GLOBAL: int = 100
    AUTO_CONFIRM_CLEAN: bool = False
    OCR_MODEL: str = "mistral-ocr-4-0"
    EXTRACTION_MODEL: str = "mistral-small-2603"
    MONTHLY_AI_BUDGET_USD: float = 25.0
    MONTHLY_TOTAL_BUDGET_USD: float = 0.0
    OCR_USD_PER_1000_PAGES: float = 4.0
    LLM_INPUT_USD_PER_MILLION_TOKENS: float = 0.15
    LLM_OUTPUT_USD_PER_MILLION_TOKENS: float = 0.60
    HOSTING_MONTHLY_COST_USD: float = 0.0
    SUPABASE_MONTHLY_COST_USD: float = 0.0
    OTHER_MONTHLY_COST_USD: float = 0.0
    RUNTIME_DIR: str = "runtime"

    @property
    def database_key(self) -> str:
        """El backend usa service_role; anon solo se admite en desarrollo legado."""
        return self.SUPABASE_SERVICE_ROLE_KEY or self.SUPABASE_ANON_KEY

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
        """Lista de IDs autorizados, validada al construir Settings."""
        return [int(uid.strip()) for uid in self.TELEGRAM_ALLOWED_USERS.split(",")]

    @model_validator(mode="after")
    def validate_required_keys(self) -> "Settings":
        required = ["MISTRAL_API_KEY", "SUPABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"]
        if self.ENVIRONMENT.lower() == "production":
            required.append("SUPABASE_SERVICE_ROLE_KEY")
        elif not self.database_key:
            required.append("SUPABASE_ANON_KEY")
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

        raw_users = [part.strip() for part in self.TELEGRAM_ALLOWED_USERS.split(",")]
        invalid_users = [part for part in raw_users if not part.isdigit() or int(part) <= 0]
        if invalid_users:
            raise ValueError(
                "TELEGRAM_ALLOWED_USERS debe contener uno o más IDs numéricos positivos "
                "separados por comas. El acceso abierto no está permitido."
            )

        if self.ENVIRONMENT.lower() not in {"development", "test", "staging", "production"}:
            raise ValueError("ENVIRONMENT debe ser development, test, staging o production")
        if self.ENVIRONMENT.lower() == "production" and self.AUTO_CONFIRM_CLEAN:
            raise ValueError(
                "AUTO_CONFIRM_CLEAN debe permanecer false en producción: un propietario "
                "debe confirmar cada documento antes de publicarlo"
            )
        if not (1 <= self.MAX_PENDING_PER_USER <= self.MAX_PENDING_GLOBAL <= 10_000):
            raise ValueError("Los límites de cola no son válidos")
        if not (1024 <= self.MAX_DOCUMENT_BYTES <= 50 * 1024 * 1024):
            raise ValueError("MAX_DOCUMENT_BYTES debe estar entre 1 KB y 50 MB")
        if self.MONTHLY_AI_BUDGET_USD <= 0:
            raise ValueError("MONTHLY_AI_BUDGET_USD debe ser positivo")
        if min(
            self.HOSTING_MONTHLY_COST_USD,
            self.SUPABASE_MONTHLY_COST_USD,
            self.OTHER_MONTHLY_COST_USD,
            self.MONTHLY_TOTAL_BUDGET_USD,
        ) < 0:
            raise ValueError("Los costes fijos y presupuestos mensuales no pueden ser negativos")

        return self


settings = Settings()
