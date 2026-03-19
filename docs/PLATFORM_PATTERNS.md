# Platform Patterns

Recommended conventions for platform apps. Inspired by Human Index and current fleet patterns, but intended as the target standard for new work and debt reduction rather than a verbatim description of any single app.

**Target stack**: FastAPI + SQLAlchemy 2.0 + Alembic + Postgres, React 19 + TypeScript 5.9 + Vite 7 + React Router 7, usually as a single Docker container serving the SPA from FastAPI.

---

## Table of Contents

**Backend**
1. [Router Structure](#1-router-structure)
2. [Models & Database](#2-models--database)
3. [Schemas (Pydantic v2)](#3-schemas-pydantic-v2)
4. [Config & Settings](#4-config--settings)
5. [Dependencies & Auth](#5-dependencies--auth)
6. [Services](#6-services)
7. [Error Handling](#7-error-handling)
8. [Pagination & Lists](#8-pagination--lists)
9. [AI Integration](#9-ai-integration)
10. [Cross-Cutting Concerns](#10-cross-cutting-concerns)

**Frontend**
11. [API Client](#11-api-client)
12. [Routing](#12-routing)
13. [Component Patterns](#13-component-patterns)
14. [Styling](#14-styling)
15. [Testing](#15-testing)

**Infrastructure**
16. [Deploy & Docker](#16-deploy--docker)
17. [Import Conventions](#17-import-conventions)
18. [App Setup (main.py)](#18-app-setup-mainpy)
19. [Database Migrations (Alembic)](#19-database-migrations-alembic)

---

## 1. Router Structure

One domain per file. Router declares its own prefix and tags.

```python
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import check_tier_limit, get_current_user
from app.models import Widget, User
from app.schemas import WidgetCreateIn, WidgetUpdateIn, WidgetOut

router = APIRouter(prefix="/widgets", tags=["widgets"])
```

**CRUD shape:**

```python
# List
@router.get("", response_model=list[WidgetOut])
def list_widgets(
    q: str | None = Query(default=None, min_length=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Widget).where(Widget.user_id == user.id)
    if q:
        stmt = stmt.where(Widget.name.ilike(f"%{q}%"))
    stmt = stmt.order_by(Widget.created_at.desc())
    rows = db.scalars(stmt).all()
    return [_to_widget_out(row) for row in rows]


# Create (tier-gated)
@router.post("", response_model=WidgetOut, status_code=status.HTTP_201_CREATED)
def create_widget(
    payload: WidgetCreateIn,
    user: User = Depends(check_tier_limit("widgets")),
    db: Session = Depends(get_db),
):
    widget = Widget(
        user_id=user.id,
        name=payload.name.strip(),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(widget)
    db.commit()
    db.refresh(widget)
    return _to_widget_out(widget)


# Read
@router.get("/{widget_id}", response_model=WidgetOut)
def get_widget(
    widget_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    widget = get_widget_or_404(db, user=user, widget_id=widget_id)
    return _to_widget_out(widget)


# Update (selective PATCH)
@router.patch("/{widget_id}", response_model=WidgetOut)
def update_widget(
    widget_id: uuid.UUID,
    payload: WidgetUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    widget = get_widget_or_404(db, user=user, widget_id=widget_id)
    if payload.name is not None:
        widget.name = payload.name.strip()
    if "description" in payload.model_fields_set:
        widget.description = payload.description
    widget.updated_at = utcnow()
    db.add(widget)
    db.commit()
    db.refresh(widget)
    return _to_widget_out(widget)


# Delete
@router.delete("/{widget_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_widget(
    widget_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    widget = get_widget_or_404(db, user=user, widget_id=widget_id)
    db.delete(widget)
    db.commit()
```

**Key rules:**
- Never return raw ORM models. Use private `_to_*_out()` helpers.
- Use `payload.model_fields_set` to distinguish "field was sent as null" from "field was omitted".
- Background work: `background_tasks.add_task(fn, id=widget.id)` — pass IDs, not objects.

> **Source**: `human-index/backend/app/routers/subjects.py`

---

## 2. Models & Database

SQLAlchemy 2.0 with `Mapped[T]` type annotations. Queries use `select()` style.

### Model conventions

```python
import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid
from app.database import Base


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Widget(Base):
    __tablename__ = "widgets"

    # UUID primary key
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Multi-tenancy: tenant-scoped domain tables carry an owner key
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # Timestamp pair: created_at (immutable) + updated_at (mutable)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Soft delete by default for user-facing domain records
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship()

    # Composite constraints via __table_args__
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_widgets_user_name"),
    )
```

### Session factory

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings

connect_args: dict[str, object] = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

### Query style (2.0)

```python
from sqlalchemy import select, func

# Single row
stmt = select(Widget).where(Widget.id == widget_id, Widget.user_id == user.id)
widget = db.scalars(stmt).first()

# List
stmt = select(Widget).where(Widget.user_id == user.id).order_by(Widget.created_at.desc())
rows = db.scalars(stmt).all()

# Count
stmt = select(func.count(Widget.id)).where(Widget.user_id == user.id)
count = db.execute(stmt).scalar() or 0

# Join
stmt = (
    select(Attachment, Observation)
    .join(Observation, Observation.id == Attachment.observation_id)
    .where(Attachment.user_id == user.id)
    .order_by(Observation.observed_at.desc())
    .offset(offset).limit(limit)
)
rows = db.execute(stmt).all()
```

**Key rules:**
- `autocommit=False` — always explicit `db.commit()`.
- Tenant-scoped domain tables should carry an explicit owner key such as `user_id`; system tables, lookup tables, and rate-limit/audit helpers may not need one.
- Soft delete is the default for user-facing domain records. Use hard delete only when retention adds more operational or product cost than value.
- Relationships use `cascade="all, delete-orphan"` on parent side when child lifecycle is owned by the parent.
- Foreign keys use `ondelete="CASCADE"` and `index=True` when that matches the desired lifecycle and query pattern.

> **Source**: `human-index/backend/app/models.py`, `database.py`

---

## 3. Schemas (Pydantic v2)

Three-shape pattern: `*CreateIn` / `*UpdateIn` / `*Out`.

```python
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

WidgetStatus = Literal["active", "archived", "draft"]


# Create: required fields
class WidgetCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: WidgetStatus = "draft"


# Update: all optional
class WidgetUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: WidgetStatus | None = None


# Output: full + computed fields
class WidgetOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: WidgetStatus
    created_at: datetime
    updated_at: datetime


# Nested output for relationships
class WidgetSummaryOut(BaseModel):
    id: uuid.UUID
    name: str

class ProjectOut(BaseModel):
    id: uuid.UUID
    title: str
    widgets: list[WidgetSummaryOut]
```

**Key rules:**
- Use `Literal[...]` unions for enums, not Python `Enum` classes.
- `Out` schemas include `id`, timestamps, and derived/computed fields.
- `model_config = ConfigDict(...)` for Pydantic v2 (not `class Config:`).

> **Source**: `human-index/backend/app/schemas.py`

---

## 4. Config & Settings

```python
from pydantic import AliasChoices, ConfigDict, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = ConfigDict(env_file=".env", env_file_encoding="utf-8")

    environment: str = "dev"  # dev|test|prod
    database_url: str = "sqlite:///./data/app.db"

    # Session
    session_cookie_name: str = "app_session"
    session_ttl_days: int = 30
    csrf_cookie_name: str = "app_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    # CORS
    cors_origins: list[str] = ["http://localhost:5173"]

    # API keys with alias choices (support both prefixed and unprefixed)
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "MY_APP_OPENAI_API_KEY"),
    )

    # Optional integrations
    sentry_dsn: str | None = None


settings = Settings()

# Tier limits — None means unlimited
TIER_LIMITS: dict[str, dict[str, int | None]] = {
    "free": {"widgets": 10, "projects": 3},
    "pro": {"widgets": None, "projects": None},
}


def cookie_secure() -> bool:
    return settings.environment in ("prod", "production")
```

**Key rules:**
- `AliasChoices` for env var flexibility across projects.
- `str | None` for optional secrets — don't crash if missing.
- Module-level `settings = Settings()` singleton.

> **Source**: `human-index/backend/app/config.py`

---

## 5. Dependencies & Auth

Cascading dependency injection for auth and authorization.

**Supported auth modes:**
- **Session/cookie auth** for user-facing SPAs where a browser is the main client.
- **Bearer token auth** for personal access tokens, delegated access, or mixed browser/API clients.
- **API-key auth** for agent-first or service-to-service apps where the primary client is another tool, runner, or automation.

Choose the simplest mode that matches the product surface. Do not force session auth onto machine clients, and do not force API-key auth onto end-user browser flows that need CSRF protection and session UX.

```python
from fastapi import Depends, HTTPException, Request, status

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    # 1. Try OAuth (Spark Swarm)
    # 2. Try Bearer token (PAT)
    # 3. Try session cookie
    # 4. Raise 401
    ...

def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user

def require_pro(user: User = Depends(get_current_user)) -> User:
    if user.account_tier != "pro":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "pro_required", "tier": user.account_tier},
        )
    return user

def check_tier_limit(resource: str):
    """Factory: returns a dependency that checks tier limits before creation."""
    def dependency(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        limits = TIER_LIMITS.get(user.account_tier, TIER_LIMITS["free"])
        limit = limits.get(resource)
        if limit is None:
            return user
        current = _count_resource(db, user.id, resource)
        if current >= limit:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "tier_limit_reached",
                    "resource": resource,
                    "limit": limit,
                    "current": current,
                    "tier": user.account_tier,
                },
            )
        return user
    return dependency
```

### API-key auth pattern

For agent-first apps, prefer a dedicated dependency that resolves the caller from `X-API-Key`.

```python
from fastapi import Depends, Header, HTTPException, status

def get_api_actor(
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiActor:
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key required")

    key_hash = sha256_hex(x_api_key)
    actor = lookup_api_actor_by_key_hash(db, key_hash=key_hash)
    if actor is None or actor.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    record_api_key_use(db, actor_id=actor.id)
    return actor
```

**Security patterns:**
- Hash tokens with `sha256_hex()` before storing in DB.
- Hash API keys before storing them; never persist raw keys after issuance.
- API keys should have a visible prefix or key ID for audit/debug, plus scope, status, created_at, last_used_at, and optional expires_at.
- Prefer scoped API keys over one global secret when an app is expected to be used by multiple agents, automations, or environments.
- API-key apps should still enforce authorization in dependencies/services; authentication alone is not enough.
- Apply rate limits per key or per actor on externally exposed machine endpoints.
- CSRF: cookie + header match on non-safe methods, skipped for Bearer auth.
- Session rolling: extend TTL on each authenticated request.
- `secrets.compare_digest()` for constant-time comparison.

> **Source**: `human-index/backend/app/deps.py`, `security.py`, `spark-swarm/backend/app/routers/auth.py`, `spark-swarm/backend/app/routers/secrets.py`

---

## 6. Services

Business logic lives in services, not routers.

```python
from sqlalchemy import select

def get_widget_or_404(
    db: Session, *, user: User, widget_id: uuid.UUID
) -> Widget:
    stmt = select(Widget).where(Widget.id == widget_id, Widget.user_id == user.id)
    widget = db.scalars(stmt).first()
    if not widget:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return widget
```

**Key rules:**
- `get_*_or_404()` helpers encapsulate query + 404.
- Normalization functions isolate data cleaning.
- Complex queries and multi-step logic live in services.
- Preserve input order when returning results from queries.

> **Source**: `human-index/backend/app/services/`

---

## 7. Error Handling

```python
# Simple errors
raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests.")

# Structured errors (for client-side handling)
raise HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail={
        "error": "tier_limit_reached",
        "resource": "widgets",
        "limit": 10,
        "current": 10,
        "tier": "free",
    },
)

# Conflict with IntegrityError
try:
    db.commit()
except IntegrityError as exc:
    db.rollback()
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="That record already exists.",
    ) from exc
```

**Status code cheat sheet:** 400 validation, 401 authn, 403 authz/tier, 404 not found, 409 conflict, 429 rate limit.

---

## 8. Pagination & Lists

```python
@router.get("", response_model=list[WidgetOut])
def list_widgets(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="created_at_desc"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Whitelist sort values — never pass arbitrary strings
    if sort_by not in {"created_at_desc", "name_asc"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sort_by")

    stmt = select(Widget).where(Widget.user_id == user.id)
    if sort_by == "name_asc":
        stmt = stmt.order_by(Widget.name.asc())
    else:
        stmt = stmt.order_by(Widget.created_at.desc())

    rows = db.scalars(stmt.offset(offset).limit(limit)).all()
    return [_to_widget_out(row) for row in rows]
```

**Key rules:**
- Always bound `limit` with `ge=1, le=MAX`.
- Optional filters default to `None` (skip filter when absent).
- Never pass user input directly to `.order_by()`.

---

## 9. AI Integration

### Provider abstraction

```python
@dataclass(frozen=True, slots=True)
class TextOutput:
    parsed: dict[str, Any]
    usage: TextUsage
    tool_calls: list[TextToolCall] | None = None
    raw_response: Any | None = None

@dataclass(frozen=True, slots=True)
class TextUsage:
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None

def build_text_provider(provider_slug: str) -> TextJsonProvider | None:
    """Factory: returns None if API key is missing (graceful unavailability)."""
    if provider_slug == "openai" and settings.openai_api_key:
        return OpenAITextJsonProvider(api_key=settings.openai_api_key)
    if provider_slug == "anthropic" and settings.anthropic_api_key:
        return AnthropicProvider(api_key=settings.anthropic_api_key)
    return None
```

### Usage tracking

Every AI call writes an audit trail:

```python
# Record the run
run = AiInferenceRun(
    user_id=user.id,
    feature="profile",
    provider="openai",
    model="gpt-5-mini",
    status="success",
    input_tokens=output.usage.input_tokens,
    output_tokens=output.usage.output_tokens,
    cost_usd_micros=calculate_cost_usd_micros(db, ...),
)
db.add(run)
```

### Cost calculation

```python
def calculate_cost_usd_micros(db, *, provider, model, occurred_at, usage) -> int:
    card = _lookup_price_card(db, provider=provider, model=model, occurred_at=occurred_at)
    if card is None:
        return 0
    non_cached_input = max((usage.input_tokens or 0) - (usage.cached_input_tokens or 0), 0)
    input_cost = (non_cached_input * card.input_per_million_usd_micros) // 1_000_000
    output_cost = ((usage.output_tokens or 0) * card.output_per_million_usd_micros) // 1_000_000
    cached_cost = ((usage.cached_input_tokens or 0) * card.cached_input_per_million_usd_micros) // 1_000_000
    return input_cost + output_cost + cached_cost
```

**Key rules:**
- Graceful unavailability: return `None` if no API key, don't crash.
- Cache invalidation: SHA256 fingerprint of sources + context; skip AI call if unchanged.
- Prompt discipline: enforce JSON schema in prompt, require evidence citations.
- Background AI tasks open their own DB session.

> **Source**: `human-index/backend/app/ai/`

---

## 10. Cross-Cutting Concerns

### Storage abstraction

```python
def upload_bytes(storage_path: str, data: bytes, content_type: str) -> None:
    if settings.media_backend == "spaces":
        client = _spaces_client()
        client.put_object(
            Bucket=settings.spaces_bucket,
            Key=media_key(storage_path),
            Body=data,
            ContentType=content_type,
        )
        return
    full_path = Path(settings.attachments_dir) / storage_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(data)

def read_bytes(storage_path: str) -> bytes:
    if settings.media_backend == "spaces":
        client = _spaces_client()
        obj = client.get_object(Bucket=settings.spaces_bucket, Key=media_key(storage_path))
        return obj["Body"].read()
    return (Path(settings.attachments_dir) / storage_path).read_bytes()
```

### Email (mode-switched)

```python
# Settings: MAIL_MODE = "postmark" | "stdout" | "noop"
# - postmark: send via Postmark API (prod)
# - stdout: print to console (local dev)
# - noop: do nothing (tests, ephemeral staging)
```

### Logging

```python
class JSONFormatter(logging.Formatter):
    """JSON lines in prod. Human-readable in dev."""
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "user_id", "method", "path", "status_code", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str)

def configure_logging(environment: str) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    if environment in ("prod", "production"):
        handler.setFormatter(JSONFormatter())
        root.setLevel(logging.INFO)
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s"))
        root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    # Quiet noisy third-party loggers
    for name in ("uvicorn.access", "sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
```

### Error monitoring

```python
# Sentry is optional, lazy-imported, non-fatal if missing
def init_error_monitoring() -> None:
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment)
    except Exception:
        logger.warning("Sentry DSN configured but sentry_sdk is not installed")
```

> **Source**: `human-index/backend/app/storage.py`, `email.py`, `logging_config.py`, `error_monitoring.py`

---

## 11. API Client

Type-safe fetch wrapper with CSRF and error classification.

### Core fetch

```typescript
export class ApiError extends Error {
  status: number;
  body: string;
  constructor(status: number, message: string, body: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  const headers = withCsrfHeader(method, {
    "Content-Type": "application/json",
    ...(init?.headers ?? {}),
  });
  let res: Response;
  try {
    res = await fetch(path, { credentials: "include", headers, ...init });
  } catch {
    throw new ApiError(0, "Network error", "");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new ApiError(res.status, errorMessageForStatus(res.status, text), text);
  }
  return (await res.json()) as T;
}
```

### CSRF

```typescript
const SAFE_HTTP_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

function withCsrfHeader(method: string | undefined, headers?: HeadersInit): Headers {
  const merged = new Headers(headers ?? {});
  if (SAFE_HTTP_METHODS.has((method ?? "GET").toUpperCase())) return merged;
  const token = getCookieValue("app_csrf");
  if (token && !merged.has("X-CSRF-Token")) merged.set("X-CSRF-Token", token);
  return merged;
}
```

### API call patterns

```typescript
// Simple GET
export async function getMe() {
  return apiFetch<AuthMe>("/api/v1/auth/me");
}

// POST with payload
export async function createWidget(payload: { name: string }) {
  return apiFetch<Widget>("/api/v1/widgets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// GET with query params
export async function listWidgets(opts?: { limit?: number; offset?: number; q?: string }) {
  const params = new URLSearchParams();
  if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts?.offset !== undefined) params.set("offset", String(opts.offset));
  if (opts?.q?.trim()) params.set("q", opts.q.trim());
  const query = params.toString();
  return apiFetch<Widget[]>(query ? `/api/v1/widgets?${query}` : "/api/v1/widgets");
}

// File upload (skip apiFetch — use raw fetch + FormData, no Content-Type)
export async function uploadFile(widgetId: string, file: File) {
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(`/api/v1/widgets/${widgetId}/attachments`, {
    method: "POST",
    credentials: "include",
    headers: withCsrfHeader("POST"),
    body,
  });
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as Attachment;
}
```

### Error detection helpers

```typescript
export function isAuthError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status === 401) return true;
  if (error.status === 403 && !isTierBody(error.body)) return true;
  return false;
}

export function isTierError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403 && isTierBody(error.body);
}
```

> **Source**: `human-index/frontend/src/api.ts`

---

## 12. Routing

```tsx
import { createBrowserRouter, Navigate, type RouteObject } from "react-router-dom";

export const routeConfig: RouteObject[] = [
  // Public routes (no layout wrapper)
  { path: "/", element: <LandingPage /> },

  // Auth routes
  {
    path: "/login",
    element: <AuthLayout><LoginPage /></AuthLayout>,
  },

  // Protected routes (Layout checks getMe())
  {
    element: <Layout />,
    children: [
      { path: "dashboard", element: <DashboardPage /> },
      { path: "widgets", element: <WidgetsPage /> },
      { path: "widgets/:widgetId", element: <WidgetPage /> },
      { path: "settings", element: <SettingsPage /> },
    ],
  },

  // Catch-all
  { path: "*", element: <Navigate to="/" replace /> },
];

export const router = createBrowserRouter(routeConfig);
```

> **Source**: `human-index/frontend/src/router.tsx`

---

## 13. Component Patterns

### Data fetching with cleanup

```tsx
export function WidgetPage() {
  const { widgetId } = useParams();
  const navigate = useNavigate();
  const [widget, setWidget] = useState<Widget | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!widgetId) return;
    let active = true;  // Prevents state updates after unmount
    getWidget(widgetId)
      .then((w) => { if (active) setWidget(w); })
      .catch((err) => {
        if (!active) return;
        if (isAuthError(err)) navigate("/login");
        else setError((err as Error).message);
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [widgetId, navigate]);

  if (loading) return <PageSkeleton />;
  if (error) return <div className="error">{error}</div>;
  if (!widget) return null;
  return <div>...</div>;
}
```

### Form submission

```tsx
async function onSubmit(e: FormEvent) {
  e.preventDefault();
  setError(null);
  try {
    const widget = await createWidget({ name });
    setName("");
    navigate(`/widgets/${widget.id}`);
  } catch (e) {
    if (isAuthError(e)) { navigate("/login"); return; }
    setError(getErrorMessage(e, "Could not create widget."));
  }
}
```

### LoadingButton

```tsx
export function LoadingButton({ loading = false, children, disabled, ...rest }: LoadingButtonProps) {
  return (
    <button className="loadingButton" disabled={disabled || loading} {...rest}>
      {loading ? <><span className="btnSpinner" aria-hidden="true" />{children}</> : children}
    </button>
  );
}
```

### Auto-dismiss messages

```tsx
export function useAutoDismiss(
  value: string | null,
  setValue: Dispatch<SetStateAction<string | null>>,
  timeoutMs = 4000,
) {
  useEffect(() => {
    if (!value) return;
    const id = window.setTimeout(() => setValue(null), timeoutMs);
    return () => window.clearTimeout(id);
  }, [value, setValue, timeoutMs]);
}
```

**Key rules:**
- Three states: `loading` / `error` / data.
- Auth redirect: catch `isAuthError(err)` → `navigate("/login")`.
- State: prop drilling + callbacks (no global store needed for most apps).
- Flat structure: all components in `ui/`, one `.tsx` per component.

> **Source**: `human-index/frontend/src/ui/`

---

## 14. Styling

Plain CSS with custom properties is the default. Start with a shared `styles.css` and split later only when that improves maintainability.

```css
:root {
  --bg: #ffffff;
  --ink: #1a1a1a;
  --muted: #6b7280;
  --line: #e5e7eb;
  --accent: #3b82f6;
}

[data-theme="dark"] {
  --bg: #0f0f0f;
  --ink: #e5e5e5;
  --muted: #9ca3af;
  --line: #2d2d2d;
  --accent: #60a5fa;
}

body {
  background: var(--bg);
  color: var(--ink);
}
```

**Key rules:**
- Default to plain CSS plus CSS custom properties.
- Dark mode via `[data-theme="dark"]` selectors.
- Start with a shared `styles.css`; split styles or add tooling only when style isolation, bundle organization, or team coordination becomes a recurring problem.

> **Source**: `human-index/frontend/src/styles.css`

---

## 15. Testing

### Backend (pytest)

```bash
make test-backend
# Runs: DATABASE_URL=sqlite:///./data/test.db pytest tests/ -v
```

### Frontend (vitest + React Testing Library)

```typescript
// Mock API module
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, getMe: vi.fn(), listWidgets: vi.fn() };
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getMe).mockRejectedValue(new Error("unauthenticated"));
});

// Router testing
function renderApp(path: string) {
  const memoryRouter = createMemoryRouter(routeConfig, { initialEntries: [path] });
  render(<RouterProvider router={memoryRouter} />);
}

it("redirects to login when unauthenticated", async () => {
  renderApp("/dashboard");
  await waitFor(() => expect(screen.getByText("Sign in")).toBeTruthy());
});
```

---

## 16. Deploy & Docker

### Dockerfile (two-stage)

```dockerfile
FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY backend/pyproject.toml backend/uv.lock* ./backend/
RUN cd backend && uv sync --frozen --no-dev
COPY backend/ ./backend/
COPY --from=frontend-build /frontend/dist ./backend/app/static/
WORKDIR /app/backend
EXPOSE 8000
CMD ["bash", "-lc", "./docker-entrypoint.sh"]
```

### Entrypoint

```bash
#!/bin/bash
set -e
mkdir -p data
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Keep the entrypoint focused on process startup. Do not make schema changes on container boot in shared environments.

### Deploy-time migrations

Run migrations as a one-off deploy step before restarting the long-running service:

```bash
docker compose run --rm myapp-backend alembic upgrade head
docker compose up -d myapp-backend myapp-web
```

### Makefile

```makefile
install:
	cd backend && uv sync
	cd frontend && npm install

dev: migrate
	@$(MAKE) -j2 backend-server frontend

check: lint format-check typecheck test

migrate:
	@mkdir -p backend/data
	cd backend && uv run alembic upgrade head

test-backend:
	cd backend && DATABASE_URL=sqlite:///./data/test.db uv run pytest tests/ -v

test-frontend:
	cd frontend && npx vitest run

new-migration:
	cd backend && uv run alembic revision --autogenerate -m "$(MSG)"
```

### Health checks

```python
@app.get("/healthz")
def healthz():
    ok, detail = _db_check()
    if ok:
        return JSONResponse(status_code=200, content={"status": "ok", "db": "ok"})
    return JSONResponse(status_code=503, content={"status": "degraded", "db": "error"})

@app.get("/api/v1/healthz")
def api_healthz():
    # Same as above — two paths for flexibility
    ...
```

### Version locks

- `.node-version` → `20`
- `.python-version` → `3.12`

> **Source**: `human-index/Dockerfile`, `Makefile`, `deploy/pack.toml`

---

## 17. Import Conventions

```python
import logging                            # Standard library
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends     # Third-party
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings           # Local imports
from app.database import get_db
from app.models import Widget, User
from app.schemas import WidgetOut

logger = logging.getLogger(__name__)      # Module logger last
```

Prefer avoiding `from __future__ import annotations` in new Python 3.12+ modules unless it clearly simplifies a typing edge case or preserves consistency within an existing file.

---

## 18. App Setup (main.py)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.environment)
    init_error_monitoring()
    logger.info("App starting", extra={"environment": settings.environment})
    yield
    logger.info("App shutting down")

app = FastAPI(title="My App", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", settings.csrf_header_name],
)

# Router registration (versioned)
app.include_router(widgets.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")

# CSRF middleware
@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    has_cookie_auth = bool(request.cookies.get(settings.session_cookie_name))
    method = request.method.upper()
    if method not in {"GET", "HEAD", "OPTIONS", "TRACE"} and has_cookie_auth:
        csrf_cookie = request.cookies.get(settings.csrf_cookie_name)
        header_value = request.headers.get(settings.csrf_header_name)
        if not csrf_cookie or not header_value or not secrets.compare_digest(csrf_cookie, header_value):
            return JSONResponse(status_code=403, content={"detail": "CSRF token missing or invalid"})
    response = await call_next(request)
    # Set CSRF cookie on first authenticated GET
    if has_cookie_auth and not request.cookies.get(settings.csrf_cookie_name) and method == "GET":
            _set_csrf_cookie(response)
    return response

# Security headers
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# SPA catch-all (when static dir exists from Docker build)
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        file_path = STATIC_DIR / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(STATIC_DIR / "index.html")
```

> **Source**: `human-index/backend/app/main.py`

---

## 19. Database Migrations (Alembic)

### Naming convention

Migration files are **sequentially numbered** with zero-padded prefixes:

```
migrations/versions/
  0000_initial.py
  0001_add_widgets_table.py
  0002_add_widget_status_column.py
```

### Creating migrations

Always use the Makefile target (never run `alembic revision` directly):

```bash
make new-migration MSG="add widgets table"
```

Migrations run automatically in the Docker entrypoint before the app starts.

> **Source**: `human-index/AGENTS.md`

---

## When to Deviate

**General:**
- **SQLite-only apps** (e.g., single-user tools): skip Alembic, use `Base.metadata.create_all()`.
- **No auth needed**: drop deps.py, CSRF middleware, session management.
- **Agent-first apps**: API-key auth is a first-class option; use `X-API-Key` plus hashed/scoped keys instead of sessions.
- **No AI features**: skip section 9 entirely.
- **No-database apps** (e.g., richmiles.xyz): FastAPI backend that proxies data from external APIs (Spark Swarm) with static fallback — skip sections 2, 8, 19 (models, pagination, migrations).
- **Different frontend** (e.g., HTMX, no SPA): skip sections 11-15, adjust Dockerfile.

**Project-specific exceptions:**
- **Esher's Codex**: no auth system — single learner identified by httpOnly cookie.
- **IEOMD**: has additional zero-knowledge crypto requirements (encryption keys in URL fragment only, never sent to server).
