from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_key: str = ""
    spine_dataset: str = "spine"
    spine_data_dir: str = "data/incoming"
    system_root_directory: str = ".spine/system"
    data_root_directory: str = ".spine/data"
    cache_root_directory: str = ".spine/cache"

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def resolve_data_dir(self) -> Path:
        p = Path(self.spine_data_dir)
        return p if p.is_absolute() else self.project_root / p


settings = Settings()
