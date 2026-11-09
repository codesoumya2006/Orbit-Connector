from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.routes import router
from app.api.organizations import router as organizations_router
from app.api.sync_jobs import router as sync_jobs_router
from app.api.google_analytics import router as google_analytics_router
from app.api.google_ads import router as google_ads_router
from app.api.meta_ads import router as meta_ads_router
from app.api.supermetrics import router as supermetrics_router
from app.api.groq import router as groq_router

allowed_origins = list(
    dict.fromkeys(
        (
            settings.frontend_origin,
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3001",
        )
    )
)


class CORSFastAPI(FastAPI):
    def build_middleware_stack(self):
        # Wrap ServerErrorMiddleware so unhandled errors receive CORS headers too.
        return CORSMiddleware(
            super().build_middleware_stack(),
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Organization-ID"],
        )


app = CORSFastAPI(title=settings.app_name, version="0.1.0", description="Multi-tenant integration and custom connector starter API")
app.include_router(router, prefix="/api/v1")
app.include_router(organizations_router, prefix="/api/v1")
app.include_router(sync_jobs_router, prefix="/api/v1")
app.include_router(google_analytics_router, prefix="/api/v1")
app.include_router(google_ads_router, prefix="/api/v1")
app.include_router(meta_ads_router, prefix="/api/v1")
app.include_router(supermetrics_router, prefix="/api/v1")
app.include_router(groq_router, prefix="/api/v1")
