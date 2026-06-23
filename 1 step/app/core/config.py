from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # App
    # ------------------------------------------------------------------
    debug: bool = Field(
        default=False,
        validation_alias="app_debug"
    )
    app_name: str = "Dispatch System"

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/dispatch_db"
    )

    # ------------------------------------------------------------------
    # Google Maps
    # ------------------------------------------------------------------
    google_maps_api_key: str = ""

    # ------------------------------------------------------------------
    # Dispatch loop tuning
    # ------------------------------------------------------------------
    dispatch_loop_interval_seconds: int = 30
    dispatch_window_minutes: int = 10       # HOLD threshold
    driver_stale_threshold_minutes: int = 5 # Exclude from candidates if silent

    # ------------------------------------------------------------------
    # Prep time estimator (Step 3 values)
    # ------------------------------------------------------------------
    prep_time_minutes: int = 15
    safety_buffer_minutes: int = 3


settings = Settings()
