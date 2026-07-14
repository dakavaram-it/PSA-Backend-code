from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "pa-track-workflow-api"
    app_env: str = "local"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    dakavara_db_host: str
    dakavara_db_port: int = 3306
    dakavara_db_user: str
    dakavara_db_password: str
    dakavara_db_name: str = "dakavara_pa"

    pa_track_db_host: str
    pa_track_db_port: int = 3306
    pa_track_db_user: str
    pa_track_db_password: str
    pa_track_db_name: str = "pa_track"

    report_ratings_db_host: str | None = None
    report_ratings_db_port: int = 3306
    report_ratings_db_user: str | None = None
    report_ratings_db_password: str | None = None
    report_ratings_db_name: str = "report_ratings"

    # Writable dakavara_pa account used ONLY for the caste/sub-caste update
    # (tdp_cadre.caste_state_id). Every other dakavara access stays read-only.
    update_db_host: str | None = None
    update_db_port: int = 3306
    update_db_user: str | None = None
    update_db_password: str | None = None
    update_db_name: str = "dakavara_pa"

    # SIR (Special Intensive Revision) form-count DB — lives on a separate RDS
    # instance (mytdp). Defaults below let analytics (reads) work out of the box;
    # for data entry (writes) point SIR_DB_USER/PASSWORD at a writable account.
    sir_db_host: str = "iconnect-new-prod-instance-1.cen6u6c8qawu.us-east-1.rds.amazonaws.com"
    sir_db_port: int = 3306
    sir_db_user: str = "read_only_ashok"
    sir_db_password: str = "Z#q3BB*yD+9x#ZWP"
    sir_db_name: str = "mytdp"

    # MY TDP app DB (mytdp on the main projectk cluster — feed_posts, user_points,
    # user_memberships, booth, etc. used by the candidate "MY TDP APP USAGE" panel).
    # NOTE: this is a DIFFERENT mytdp than the SIR instance above (iconnect), which
    # only mirrors voter tables and has no feed_posts. Host/user/password default to
    # the dakavara cluster + the writable (root) account, which already has mytdp
    # access; override with MYTDP_DB_* to point elsewhere.
    mytdp_db_host: str | None = None
    mytdp_db_port: int = 3306
    mytdp_db_user: str | None = None
    mytdp_db_password: str | None = None
    mytdp_db_name: str = "mytdp"

    cadre_image_base_url: str = "https://imagesearch-projectkv.s3.amazonaws.com/cadre_images"
    nominated_post_documents_base_url: str = "https://www.mypartydashboard.com/nominated_post_documents"

    cache_ttl_seconds: int = 3600
    cache_dir: str = "cache"
    default_enrollment_id: int = 2

    enable_legacy_sync: bool = False
    enable_committee_legacy_sync: bool = False

    # When True, adding a committee candidate requires a KSS membership (existing
    # flow). When False, the KSS gate is skipped and any cadre can be added.
    enable_committee_kss_check: bool = True

    @property
    def dakavara_url(self) -> str:
        return (
            f"mysql+pymysql://{self.dakavara_db_user}:{self.dakavara_db_password}"
            f"@{self.dakavara_db_host}:{self.dakavara_db_port}/{self.dakavara_db_name}"
            "?charset=utf8mb4"
        )

    @property
    def pa_track_url(self) -> str:
        return (
            f"mysql+pymysql://{self.pa_track_db_user}:{self.pa_track_db_password}"
            f"@{self.pa_track_db_host}:{self.pa_track_db_port}/{self.pa_track_db_name}"
            "?charset=utf8mb4"
        )

    @property
    def sir_url(self) -> str:
        return (
            f"mysql+pymysql://{self.sir_db_user}:{self.sir_db_password}"
            f"@{self.sir_db_host}:{self.sir_db_port}/{self.sir_db_name}"
            "?charset=utf8mb4"
        )

    # MY TDP app DB — falls back to the dakavara host + the writable (root) account,
    # both of which already point at the projectk cluster that hosts mytdp.
    @property
    def _mytdp_host(self) -> str | None:
        return self.mytdp_db_host or self.dakavara_db_host

    @property
    def _mytdp_user(self) -> str | None:
        return self.mytdp_db_user or self.update_db_user

    @property
    def _mytdp_password(self) -> str | None:
        return self.mytdp_db_password or self.update_db_password

    @property
    def mytdp_configured(self) -> bool:
        return bool(self._mytdp_host and self._mytdp_user and self._mytdp_password)

    @property
    def mytdp_url(self) -> str:
        if not self.mytdp_configured:
            raise RuntimeError("mytdp database is not configured")
        return (
            f"mysql+pymysql://{self._mytdp_user}:{self._mytdp_password}"
            f"@{self._mytdp_host}:{self.mytdp_db_port}/{self.mytdp_db_name}"
            "?charset=utf8mb4"
        )

    @property
    def update_db_configured(self) -> bool:
        return bool(
            self.update_db_host and self.update_db_user and self.update_db_password
        )

    @property
    def update_url(self) -> str:
        if not self.update_db_configured:
            raise RuntimeError("update (writable) database is not configured")
        return (
            f"mysql+pymysql://{self.update_db_user}:{self.update_db_password}"
            f"@{self.update_db_host}:{self.update_db_port}/{self.update_db_name}"
            "?charset=utf8mb4"
        )

    @property
    def report_ratings_configured(self) -> bool:
        return bool(
            self.report_ratings_db_host
            and self.report_ratings_db_user
            and self.report_ratings_db_password
        )

    @property
    def report_ratings_url(self) -> str:
        if not self.report_ratings_configured:
            raise RuntimeError("report_ratings database is not configured")
        return (
            f"mysql+pymysql://{self.report_ratings_db_user}:{self.report_ratings_db_password}"
            f"@{self.report_ratings_db_host}:{self.report_ratings_db_port}/{self.report_ratings_db_name}"
            "?charset=utf8mb4"
        )

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
