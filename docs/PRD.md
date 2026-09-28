# Dealership AI — Backend Product Requirements Document (PRD)

**Version:** 1.0
**Scope:** Backend only (REST API + database + integrations). Frontend (Next.js) and n8n workflow content are covered in separate PRDs.
**Audience:** This document is written to be handed directly to an AI coding agent (Claude) to implement feature-by-feature.

---

## How to Use This PRD

Each feature below is self-contained and implementable in isolation. Implement features in the order presented — they follow the critical dependency chain (multi-tenancy → auth → core models → integrations → billing → admin → hardening).

For every feature, the PRD specifies:
- **Purpose** — what the feature does and why it exists
- **Data model** — Prisma schema additions (tables, fields, relations, indexes)
- **API endpoints** — full route table with method, path, auth, request, response, errors
- **Business logic / acceptance criteria** — exact behavior the implementation must satisfy
- **Edge cases & validation** — what to reject and how
- **Tests** — minimum test coverage required before marking the feature done

When implementing, always:
1. Read the feature end-to-end first.
2. Add/modify the Prisma schema and run a migration.
3. Implement the service layer (business logic).
4. Implement the controller/route layer.
5. Add input validation (class-validator DTOs).
6. Write the tests listed in the feature.
7. Update the OpenAPI/Swagger spec.

---

## 1. Product Overview

**Dealership AI** is a multi-tenant SaaS that lets car dealerships automate WhatsApp customer communication using AI. Each dealership ("dealer") gets an isolated tenant with its own users, customers, leads, conversations, appointments, and billing. Customers message the dealership's WhatsApp number; an AI replies (configurable per dealer), creates leads, books appointments, and escalates to human staff when needed. The backend exposes the data and operations to (a) the dealer-facing dashboard, (b) the platform admin dashboard, (c) Twilio webhooks for inbound WhatsApp, (d) Stripe webhooks for billing, and (e) n8n for workflow orchestration.

### Primary actors
- **Dealer User** — staff at a dealership (admin, manager, agent roles). Authenticates with email/password, operates inside one tenant.
- **Platform Admin** — Anthropic-side operator. Authenticates against a separate admin auth scope, can see all tenants.
- **Customer** — end user messaging via WhatsApp. Never authenticates; identified by phone number.
- **System integrations** — Twilio (inbound WhatsApp), Stripe (billing webhooks), n8n (workflow callbacks), Google Calendar, Calendly, RDW, Autoflex DMS.

### Languages
The backend must support content in Dutch (nl) and English (en). Dealer-configurable default language per tenant; per-conversation language override based on detected customer language.

---

## 2. Tech Stack (Backend)

| Layer | Choice | Notes |
|---|---|---|
| Runtime | Node.js 20 LTS | |
| Framework | NestJS 10 (TypeScript) | Modular, DI, decorators, built-in validation |
| ORM | Prisma 5 | Migrations, type-safe queries |
| Database | PostgreSQL 15+ | One shared DB, row-level tenant isolation via `tenantId` column on every tenant-scoped table |
| Cache / queue | Redis 7 | BullMQ for background jobs |
| Auth | JWT (access + refresh) with `passport-jwt` | Access token 15 min, refresh token 30 days, rotated on use |
| Password hashing | argon2id | Pepper from env var |
| Validation | class-validator + class-transformer | DTOs for every endpoint |
| API docs | Swagger / OpenAPI 3.1 | Auto-generated from decorators; served at `/api/docs` (admin-protected in production) |
| HTTP client | axios with retry/backoff | For Twilio, Stripe, RDW, Google, Calendly, Autoflex |
| Logging | pino (JSON structured) | Correlation ID per request, redact PII fields (passwords, tokens, full phone numbers) |
| Monitoring | Sentry (errors) + OpenTelemetry (traces) | |
| Testing | Jest + Supertest | Unit + integration + e2e |
| Container | Docker (multi-stage build) | |
| CI/CD | GitHub Actions | Lint → typecheck → test → build → deploy |
| AI provider | OpenAI API (chat completions, function calling) | Model configurable per env; default `gpt-4o-mini` for fallback and `gpt-4o` for primary |

### Project structure (mandatory)

```
src/
  main.ts
  app.module.ts
  common/
    decorators/        # @CurrentUser, @CurrentTenant, @Roles, @Public
    filters/           # global exception filter
    guards/            # JwtAuthGuard, RolesGuard, TenantGuard, AdminGuard
    interceptors/      # logging, transform, timeout
    pipes/             # validation
    middleware/        # tenant resolution, correlation id, rate limit
    utils/
  config/              # env config with Joi schema validation
  prisma/
    schema.prisma
    migrations/
    seed.ts
  modules/
    auth/
    tenants/
    users/
    customers/
    leads/
    conversations/
    messages/
    appointments/
    notifications/
    notes/
    settings/
    whatsapp/          # Twilio integration
    ai/                # OpenAI orchestration
    automation/        # business hours, auto-reply rules, escalation
    workshop/          # APK, maintenance, damage flows
    sales/             # test-drive, trade-in, vehicle-inquiry flows
    integrations/
      google-calendar/
      calendly/
      rdw/
      autoflex/
      inventory/
    billing/           # Stripe
    admin/             # platform admin endpoints
    audit/             # audit logs
    health/            # /health, /ready
  jobs/                # BullMQ processors
test/
  unit/
  integration/
  e2e/
  fixtures/
```

---

## 3. Cross-Cutting Concerns (apply to every feature)

### 3.1 Multi-tenancy
- Every tenant-scoped table includes a non-nullable `tenantId` column with a foreign key to `Tenant.id` and a database index.
- A global `TenantGuard` extracts `tenantId` from the authenticated user's JWT and attaches it to `request.tenantId`.
- A Prisma middleware **must** enforce tenant scoping on every read/write of tenant-scoped models by automatically injecting `where: { tenantId }` and rejecting writes that try to set a different `tenantId`. Bypass is allowed only when the request is from a Platform Admin (separate JWT scope: `scope: 'admin'`).
- A test must exist proving that tenant A cannot read, update, or delete any record belonging to tenant B via any public endpoint.

### 3.2 Authentication & authorization
- All endpoints require JWT auth unless decorated with `@Public()`.
- JWT payload: `{ sub: userId, tenantId, role, scope: 'dealer'|'admin', jti }`.
- Roles for dealer scope: `OWNER`, `ADMIN`, `MANAGER`, `AGENT`. Enforced with `@Roles()` decorator + `RolesGuard`.
- Refresh tokens stored hashed in DB (`RefreshToken` table) with `revokedAt`, `replacedById`. Rotation on every refresh.
- All write endpoints (POST/PUT/PATCH/DELETE) must be CSRF-safe — JWT in `Authorization: Bearer` header only, no cookie auth.

### 3.3 Rate limiting
- Global: 100 req/min per IP for unauthenticated routes, 600 req/min per user for authenticated routes.
- Auth endpoints (`/auth/login`, `/auth/password-reset`): 5 req/min per IP + per email.
- Webhook endpoints: no per-request limit but signature-verified (see each integration).
- Implement via `@nestjs/throttler` with Redis store.

### 3.4 Input validation
- Every endpoint has a DTO with class-validator decorators. Reject with HTTP 400 and a structured error body:
  ```json
  { "statusCode": 400, "error": "ValidationError", "message": "...", "details": [{ "field": "email", "issue": "must be an email" }] }
  ```
- Reject unknown properties (`forbidNonWhitelisted: true`).

### 3.5 Error handling
- Global exception filter converts all errors to a uniform JSON shape:
  ```json
  { "statusCode": 404, "error": "NotFoundError", "message": "Lead not found", "correlationId": "abc-123" }
  ```
- Never leak stack traces in production responses; always log them with the correlation ID.

### 3.6 Logging & audit
- Every request logged: method, path, status, duration, tenantId, userId, correlationId.
- Every sensitive mutation (create/update/delete on Customer, Lead, Appointment, User, Settings, Billing) writes an `AuditLog` row (see Feature 21).
- PII redaction in logs: phone numbers shown as `+31******1234`; never log passwords, tokens, Stripe secrets, or full message bodies (log message ID + length instead).

### 3.7 Pagination, filtering, sorting (standard for all list endpoints)
- Query params: `?page=1&pageSize=20&sort=createdAt:desc&search=...&filter[status]=NEW`
- `pageSize` max 100, default 20.
- Response envelope:
  ```json
  {
    "data": [...],
    "meta": { "page": 1, "pageSize": 20, "total": 137, "totalPages": 7 }
  }
  ```

### 3.8 Environment variables (all required at boot; fail fast with Joi)
```
NODE_ENV
PORT
DATABASE_URL
REDIS_URL
JWT_ACCESS_SECRET
JWT_REFRESH_SECRET
JWT_ACCESS_TTL=15m
JWT_REFRESH_TTL=30d
ARGON2_PEPPER
OPENAI_API_KEY
OPENAI_MODEL_PRIMARY=gpt-4o
OPENAI_MODEL_FALLBACK=gpt-4o-mini
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_WHATSAPP_NUMBER          # default sender if dealer hasn't configured their own
TWILIO_WEBHOOK_AUTH_TOKEN       # for signature validation
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
STRIPE_PRICE_STARTER
STRIPE_PRICE_PRO
STRIPE_PRICE_ENTERPRISE
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
GOOGLE_OAUTH_REDIRECT_URI
CALENDLY_CLIENT_ID
CALENDLY_CLIENT_SECRET
CALENDLY_REDIRECT_URI
RDW_API_BASE_URL=https://opendata.rdw.nl/resource
RDW_APP_TOKEN                   # optional, raises rate limits
AUTOFLEX_API_BASE_URL
AUTOFLEX_API_KEY
SENTRY_DSN
APP_BASE_URL                    # e.g. https://api.example.com
DASHBOARD_BASE_URL              # e.g. https://app.example.com
ENCRYPTION_KEY                  # 32-byte key for at-rest encryption of OAuth tokens, dealer Twilio creds, etc.
```

### 3.9 Encryption at rest (application-level)
Sensitive per-tenant secrets stored in DB (dealer's Twilio credentials, OAuth refresh tokens for Google/Calendly, Autoflex API keys) must be encrypted with AES-256-GCM using `ENCRYPTION_KEY`. Implement an `EncryptedString` Prisma extension or a small `CryptoService` with `encrypt(plain): string` and `decrypt(cipher): string`. Never log decrypted values.

---

## 4. Database — Master Schema Overview

The full Prisma schema is built up feature by feature below. Here is the complete model list for reference. Every model except `Tenant`, `AdminUser`, `AuditLog`, and `Plan` is tenant-scoped (has `tenantId`).

| Model | Purpose |
|---|---|
| `Tenant` | The dealership |
| `Plan` | Stripe subscription plan definition |
| `User` | Dealer staff user |
| `RefreshToken` | Issued refresh tokens |
| `PasswordResetToken` | One-time password reset tokens |
| `AdminUser` | Platform admin |
| `Customer` | End customer (deduped by phone within tenant) |
| `Lead` | Sales opportunity attached to a customer |
| `Conversation` | A WhatsApp thread with a customer |
| `Message` | Individual WhatsApp message (in or out) |
| `Note` | Internal note attached to customer/lead/conversation |
| `Appointment` | Workshop or sales appointment |
| `Notification` | In-app notification for dealer users |
| `DealerSettings` | Per-tenant config (business hours, AI behavior, language, branding) |
| `AutoReplyRule` | Keyword/intent → reply template |
| `EscalationRule` | When to hand off to human |
| `WhatsAppConfig` | Per-tenant Twilio/WhatsApp Business credentials |
| `OAuthConnection` | Per-tenant OAuth tokens (Google, Calendly) |
| `IntegrationConfig` | Per-tenant Autoflex/inventory config |
| `Subscription` | Stripe subscription state |
| `Invoice` | Stripe invoice cache |
| `UsageRecord` | Metered usage per tenant per period (messages, AI tokens) |
| `FeatureFlag` | Per-tenant feature toggle override |
| `AuditLog` | Mutation audit trail |
| `WebhookEvent` | Idempotency record for inbound webhooks |

---

# PART A — FOUNDATION

---

## Feature 1: Project Bootstrap & Configuration

### Purpose
Stand up a NestJS application with strict environment validation, structured logging, a Prisma connection, a Redis connection, a health endpoint, and global cross-cutting middleware (correlation ID, exception filter, validation pipe, security headers).

### Deliverables
1. NestJS project initialized with TypeScript strict mode (`"strict": true`, `"noUncheckedIndexedAccess": true`).
2. `ConfigModule` loaded globally with a Joi schema that validates every env var listed in §3.8. Fail boot if anything is missing/invalid.
3. `PrismaModule` exposing a `PrismaService` that extends `PrismaClient` with `onModuleInit` connect and `onModuleDestroy` disconnect, plus the tenant-scoping middleware (§3.1) and a soft-delete middleware (filter out `deletedAt IS NOT NULL` on read; convert `delete` calls to `update { deletedAt: now() }` for soft-deletable models).
4. `RedisModule` exposing a connected `ioredis` client and BullMQ queue factory.
5. Global pipes/filters/interceptors:
   - `ValidationPipe({ whitelist: true, forbidNonWhitelisted: true, transform: true })`
   - `GlobalExceptionFilter` (§3.5)
   - `LoggingInterceptor` (logs request + response with correlation ID)
   - `TimeoutInterceptor` (30s default, configurable per route via `@Timeout(ms)`)
6. Helmet, CORS (allowlist `DASHBOARD_BASE_URL` and admin dashboard URL only — never `*`), compression.
7. `CorrelationIdMiddleware` — if request has `x-correlation-id` header, reuse it; else generate a UUID v4. Attach to `request.correlationId`, return in response header.
8. Swagger at `/api/docs`, gated behind admin auth in production via env flag.
9. `pino` logger with PII redaction paths configured.

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | Public | Liveness — returns `{ status: 'ok', uptime, version }` |
| GET | `/ready` | Public | Readiness — checks DB ping + Redis ping; returns 503 if either fails |
| GET | `/api/docs` | Admin in prod, Public in dev | Swagger UI |

### Acceptance criteria
- Booting with any env var missing prints the exact missing key and exits 1.
- `GET /ready` returns 503 if Postgres or Redis is down.
- Requests log a structured JSON line with `correlationId`, `method`, `path`, `statusCode`, `durationMs`.
- An unhandled error in any route returns the uniform error JSON (§3.5) and is logged with the correlation ID.

### Tests
- Unit: ConfigService rejects missing env vars.
- Integration: `/health` returns 200; `/ready` returns 503 when Prisma `$queryRaw\`SELECT 1\`` throws.
- e2e: making a request with `x-correlation-id: test-abc` returns the same header in the response.

---

## Feature 2: Tenant Model & Tenant Isolation

### Purpose
Every dealership is a `Tenant`. Establish the tenant model, the isolation guarantee at the Prisma layer, and helpers for tenant-aware code.

### Data model (Prisma)
```prisma
model Tenant {
  id            String   @id @default(cuid())
  slug          String   @unique                          // URL-safe, e.g. "example-motors"
  name          String
  email         String                                    // primary contact email
  phone         String?
  country       String   @default("NL")                   // ISO 3166-1 alpha-2
  defaultLanguage String @default("nl")                   // "nl" | "en"
  timezone      String   @default("Europe/Amsterdam")
  status        TenantStatus @default(ACTIVE)
  trialEndsAt   DateTime?
  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt
  deletedAt     DateTime?

  users         User[]
  customers     Customer[]
  leads         Lead[]
  conversations Conversation[]
  messages      Message[]
  appointments  Appointment[]
  notes         Note[]
  notifications Notification[]
  settings      DealerSettings?
  whatsappConfig WhatsAppConfig?
  oauthConnections OAuthConnection[]
  integrationConfigs IntegrationConfig[]
  subscription  Subscription?
  invoices      Invoice[]
  usageRecords  UsageRecord[]
  featureFlags  FeatureFlag[]
  autoReplyRules AutoReplyRule[]
  escalationRules EscalationRule[]

  @@index([slug])
  @@index([status])
}

enum TenantStatus {
  ACTIVE
  SUSPENDED          // admin-suspended (e.g. fraud, non-payment past grace)
  CANCELED           // explicitly closed
}
```

### Tenant-scoping Prisma middleware (mandatory)
- A list of tenant-scoped models is declared as a constant. For every operation on those models:
  - On `findUnique`, `findFirst`, `findMany`, `count`, `aggregate`, `groupBy`: inject `where.tenantId = ctx.tenantId` (using AsyncLocalStorage for context).
  - On `create`: force `data.tenantId = ctx.tenantId`. Throw if caller tries to set a different one.
  - On `createMany`: assert every record has `tenantId = ctx.tenantId` (or inject if missing).
  - On `update`, `updateMany`, `delete`, `deleteMany`, `upsert`: merge `where.tenantId = ctx.tenantId`.
- Bypass: if `ctx.bypassTenant === true` (set by `PrismaService.asAdmin(fn)`), middleware does not inject. Admin endpoints must be the only callers.

### API endpoints
None at this stage — the model is consumed by all subsequent features. Tenant CRUD is exposed via the Admin Dashboard module (Feature 17).

### Acceptance criteria
- `Tenant.slug` is unique and URL-safe (`^[a-z0-9](-?[a-z0-9])*$`, max 50 chars).
- Soft delete sets `deletedAt`; subsequent queries do not return the tenant.
- An attempt to call `prisma.customer.findMany()` without a tenant context throws `MissingTenantContextError`.
- An attempt by tenant A's authenticated request to fetch a Lead belonging to tenant B returns 404 (not 403 — do not leak existence).

### Tests
- Unit: Prisma middleware injects `tenantId` on each operation type.
- Integration: write tenant A, write tenant B, list as tenant A — see only A's data.
- Security test: encode a JWT with tenant A's ID, attempt to PATCH a tenant B record by ID — expect 404.

---

## Feature 3: Authentication — Dealer (Email/Password)

### Purpose
Email/password authentication for dealer users with JWT access + refresh tokens, password reset flow, and rotation.

### Data model
```prisma
model User {
  id            String   @id @default(cuid())
  tenantId      String
  tenant        Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  email         String
  passwordHash  String
  firstName     String
  lastName      String
  phone         String?
  role          UserRole @default(AGENT)
  status        UserStatus @default(ACTIVE)
  lastLoginAt   DateTime?
  emailVerifiedAt DateTime?
  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt
  deletedAt     DateTime?

  refreshTokens RefreshToken[]
  passwordResets PasswordResetToken[]
  notifications Notification[]
  assignedLeads Lead[]    @relation("AssignedTo")
  notes         Note[]
  auditLogs     AuditLog[]

  @@unique([tenantId, email])
  @@index([tenantId])
  @@index([email])
}

enum UserRole {
  OWNER       // billing + everything; created at signup
  ADMIN       // everything except billing + tenant deletion
  MANAGER     // can manage customers, leads, appointments, conversations; cannot manage users/settings
  AGENT       // can handle conversations and notes; read-only on leads/appointments
}

enum UserStatus {
  ACTIVE
  INVITED     // invite sent, password not set
  SUSPENDED   // disabled by admin
}

model RefreshToken {
  id            String   @id @default(cuid())
  userId        String
  user          User     @relation(fields: [userId], references: [id], onDelete: Cascade)
  tokenHash     String   @unique
  expiresAt     DateTime
  revokedAt     DateTime?
  replacedById  String?
  userAgent     String?
  ipAddress     String?
  createdAt     DateTime @default(now())

  @@index([userId])
  @@index([expiresAt])
}

model PasswordResetToken {
  id          String   @id @default(cuid())
  userId      String
  user        User     @relation(fields: [userId], references: [id], onDelete: Cascade)
  tokenHash   String   @unique
  expiresAt   DateTime
  usedAt      DateTime?
  createdAt   DateTime @default(now())

  @@index([userId])
}
```

### API endpoints

| Method | Path | Auth | Body | Response |
|---|---|---|---|---|
| POST | `/auth/register` | Public | `{ tenantName, slug, email, password, firstName, lastName }` | `201 { tenant, user, accessToken, refreshToken }` |
| POST | `/auth/login` | Public | `{ email, password }` | `200 { user, accessToken, refreshToken }` |
| POST | `/auth/refresh` | Public (refresh token in body) | `{ refreshToken }` | `200 { accessToken, refreshToken }` |
| POST | `/auth/logout` | Bearer | `{ refreshToken }` | `204` |
| POST | `/auth/logout-all` | Bearer | – | `204` (revoke all user's refresh tokens) |
| POST | `/auth/password-reset/request` | Public | `{ email }` | `204` always (do not leak existence) |
| POST | `/auth/password-reset/confirm` | Public | `{ token, newPassword }` | `204` |
| POST | `/auth/password-change` | Bearer | `{ currentPassword, newPassword }` | `204` |
| GET | `/auth/me` | Bearer | – | `200 { user, tenant }` |

### Business logic
- **Register**: creates `Tenant` + first `User` (role `OWNER`) atomically in a transaction. Slug must be unique. Sends a welcome email (queued). Starts a 14-day trial (`Tenant.trialEndsAt = now + 14d`, `Subscription` not yet created).
- **Password**: minimum 10 chars, must contain at least 1 letter and 1 digit. Argon2id hashing with pepper.
- **Login**: increments a Redis-based failed-attempt counter per `email`; after 5 fails in 15 min, lock that email for 15 min and return generic `401 Invalid credentials`. Reset on successful login. Record `lastLoginAt`. Issue access + refresh tokens.
- **Access token**: signed JWT, 15 min TTL, payload `{ sub, tenantId, role, scope: 'dealer', jti }`.
- **Refresh token**: 256-bit random, returned in plaintext, hash stored in DB (`RefreshToken.tokenHash` = SHA-256 of token). On `/auth/refresh`: look up by hash, verify not expired/revoked, revoke (set `revokedAt` + `replacedById`), issue a new pair. Detect reuse: if a revoked token is presented, revoke ALL user tokens and require re-login (token theft signal).
- **Password reset**: generate 256-bit random token, store hash, expire in 1 hour. Reset email queued. On confirm, mark used, hash new password, revoke all refresh tokens.
- **Logout**: revoke the presented refresh token only. `logout-all` revokes all of the user's refresh tokens.
- **`/auth/me`**: returns the current user and their tenant (excluding password hash, including role and tenant status).

### Validation
- `email`: RFC 5322, max 254 chars, lowercased before storage and lookup.
- `slug`: `^[a-z0-9](-?[a-z0-9]){2,49}$`.
- Reject if `Tenant.status !== 'ACTIVE'` on login (return `403 TenantSuspended` with `Subscription.status` info).

### Acceptance criteria
- Reused refresh token triggers full revocation; subsequent refresh attempts return 401.
- Login is constant-time-ish: same response time for "user not found" and "wrong password" within ±50ms (always hash the supplied password against a dummy hash if user is missing).
- Password reset tokens are single-use.

### Tests
- e2e: full register → login → refresh → logout flow.
- Security: reuse a revoked refresh token, verify all tokens revoked.
- Security: brute-force lockout after 5 attempts.
- Validation: weak password rejected; duplicate slug rejected; case-insensitive email duplicate rejected.

---

## Feature 4: Role-Based Authorization

### Purpose
Enforce role permissions consistently across all dealer-scoped endpoints.

### Implementation
- `@Roles(UserRole.OWNER, UserRole.ADMIN)` decorator + `RolesGuard` that reads `request.user.role` and returns 403 if not in the allowed set.
- Permission matrix (reference — apply per endpoint in the relevant features):

| Capability | OWNER | ADMIN | MANAGER | AGENT |
|---|---|---|---|---|
| Manage users (invite/edit/remove) | ✓ | ✓ | – | – |
| Manage billing / subscription | ✓ | – | – | – |
| Edit dealer settings | ✓ | ✓ | – | – |
| Manage integrations | ✓ | ✓ | – | – |
| Configure auto-reply / escalation rules | ✓ | ✓ | ✓ | – |
| Create/edit customers | ✓ | ✓ | ✓ | – |
| View customers | ✓ | ✓ | ✓ | ✓ |
| Create/edit/assign leads | ✓ | ✓ | ✓ | – |
| View leads | ✓ | ✓ | ✓ | ✓ |
| Send WhatsApp messages (human takeover) | ✓ | ✓ | ✓ | ✓ |
| Create/edit appointments | ✓ | ✓ | ✓ | – |
| View appointments | ✓ | ✓ | ✓ | ✓ |
| Add internal notes | ✓ | ✓ | ✓ | ✓ |
| View audit logs | ✓ | ✓ | – | – |
| Export data | ✓ | ✓ | – | – |
| Delete tenant | ✓ | – | – | – |

### Tests
- For every protected endpoint, parametrized tests assert that each role gets the expected 200/403.

---

## Feature 5: User Management (within tenant)

### Purpose
Owners/Admins can invite, list, update, suspend, and remove users in their tenant.

### Data model
Already defined in Feature 3. Add:
```prisma
model UserInvite {
  id          String   @id @default(cuid())
  tenantId    String
  email       String
  role        UserRole
  tokenHash   String   @unique
  expiresAt   DateTime
  acceptedAt  DateTime?
  invitedById String
  createdAt   DateTime @default(now())

  @@unique([tenantId, email])
  @@index([tenantId])
}
```

### API endpoints

| Method | Path | Auth | Body |
|---|---|---|---|
| GET | `/users` | OWNER/ADMIN | — list users in tenant (paginated, filter by role/status, search by name/email) |
| POST | `/users/invites` | OWNER/ADMIN | `{ email, role, firstName, lastName }` → sends invite email with link `${DASHBOARD_BASE_URL}/accept-invite?token=...` |
| GET | `/users/invites` | OWNER/ADMIN | list pending invites |
| DELETE | `/users/invites/:id` | OWNER/ADMIN | revoke invite |
| POST | `/auth/invites/accept` | Public | `{ token, password }` → creates User with status ACTIVE, returns tokens |
| GET | `/users/:id` | OWNER/ADMIN (or self) | get user |
| PATCH | `/users/:id` | OWNER/ADMIN (or self for limited fields) | `{ firstName?, lastName?, phone?, role?, status? }` |
| DELETE | `/users/:id` | OWNER (cannot delete self if last OWNER) | soft delete |

### Business logic
- Cannot demote/remove the last OWNER (return 409 `LastOwnerError`).
- Cannot change own role.
- AGENT/MANAGER cannot fetch other users' details (only the user list with basic fields: id, name, role).
- Invite token: 256-bit, 7-day expiry, single-use.
- On invite accept, create user with the email/role from the invite and the password from the request; do not allow the email to be changed at accept time.

### Tests
- Cannot demote last OWNER.
- Inviting same email twice (pending invite present) returns 409.
- Invite expiration enforced.

---

## Feature 6: DealerSettings

### Purpose
Per-tenant configuration: business hours, AI behavior, language defaults, branding, escalation contact.

### Data model
```prisma
model DealerSettings {
  id              String   @id @default(cuid())
  tenantId        String   @unique
  tenant          Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  // Branding
  logoUrl         String?
  brandColor      String?                                   // hex

  // Localization
  defaultLanguage String   @default("nl")                   // "nl" | "en"
  supportedLanguages String[] @default(["nl","en"])
  timezone        String   @default("Europe/Amsterdam")

  // Business hours: JSON of weekday → [{ open: "08:30", close: "17:30" }] in local timezone
  businessHours   Json     @default("{\"mon\":[{\"open\":\"08:30\",\"close\":\"17:30\"}],\"tue\":[{\"open\":\"08:30\",\"close\":\"17:30\"}],\"wed\":[{\"open\":\"08:30\",\"close\":\"17:30\"}],\"thu\":[{\"open\":\"08:30\",\"close\":\"17:30\"}],\"fri\":[{\"open\":\"08:30\",\"close\":\"17:30\"}],\"sat\":[],\"sun\":[]}")
  holidays        Json     @default("[]")                   // [{ date: "2026-12-25", label: "Christmas" }]

  // AI behavior
  aiEnabled       Boolean  @default(true)
  aiPersona       String?  @db.Text                         // system prompt fragment, e.g. "You are the friendly assistant of Example Motors..."
  aiTone          String   @default("friendly")             // friendly | formal | concise
  aiMaxTurnsBeforeEscalation Int @default(8)
  aiModel         String?                                   // override default model per dealer (optional)
  aiTemperature   Float    @default(0.4)

  // Escalation
  escalationEmail String?
  escalationPhone String?                                   // WhatsApp number to notify
  outOfHoursReply String?  @db.Text                         // template; if null use default

  // Notifications
  notifyOnNewLead     Boolean @default(true)
  notifyOnEscalation  Boolean @default(true)
  notifyOnNewAppointment Boolean @default(true)

  createdAt       DateTime @default(now())
  updatedAt       DateTime @updatedAt
}
```

### API endpoints

| Method | Path | Auth |
|---|---|---|
| GET | `/settings` | Any role (full read) |
| PUT | `/settings` | OWNER/ADMIN |
| PATCH | `/settings` | OWNER/ADMIN (partial update) |

### Business logic
- On tenant creation, a `DealerSettings` row is created with defaults.
- `defaultLanguage` must be in `supportedLanguages`.
- `businessHours` schema validated: keys `mon..sun`, values arrays of `{open, close}` in `HH:mm` 24h format, open < close.
- `holidays` validated as array of `{date: YYYY-MM-DD, label?}`.
- `aiPersona` max 2000 chars.
- `brandColor` validated as `^#[0-9A-Fa-f]{6}$`.

### Tests
- Settings created automatically with tenant.
- Invalid business hours rejected.
- Non-admin cannot PATCH.


# PART B — CORE DOMAIN

---

## Feature 7: Customers

### Purpose
Store end-customers of the dealership, deduplicated within a tenant by phone number. Every WhatsApp conversation is attached to a `Customer`. Dealer staff can also create/edit customers manually.

### Data model
```prisma
model Customer {
  id            String   @id @default(cuid())
  tenantId      String
  tenant        Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  phoneE164     String                                       // canonical, e.g. "+31612345678"
  firstName     String?
  lastName      String?
  email         String?
  language      String?                                      // "nl" | "en" | null (inherits tenant default)

  source        CustomerSource @default(WHATSAPP)
  tags          String[]       @default([])
  consentMarketing Boolean     @default(false)
  consentGivenAt DateTime?

  // Vehicle info (optional — populated via RDW or manually)
  licensePlate  String?                                      // Dutch kenteken; uppercased, no dashes
  vehicleVin    String?
  vehicleMake   String?
  vehicleModel  String?
  vehicleYear   Int?

  notes         Note[]
  leads         Lead[]
  conversations Conversation[]
  appointments  Appointment[]

  firstSeenAt   DateTime @default(now())
  lastSeenAt    DateTime @default(now())
  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt
  deletedAt     DateTime?

  @@unique([tenantId, phoneE164])
  @@index([tenantId, lastSeenAt])
  @@index([tenantId, email])
  @@index([tenantId, licensePlate])
}

enum CustomerSource {
  WHATSAPP
  MANUAL
  IMPORT
  WEB
}
```

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/customers` | Any | list (paginated, search by name/phone/email/plate, filter by tag/source) |
| POST | `/customers` | MANAGER+ | create manually |
| GET | `/customers/:id` | Any | detail incl. lead count, last conversation, recent appointments |
| PATCH | `/customers/:id` | MANAGER+ | partial update |
| DELETE | `/customers/:id` | ADMIN+ | soft delete (cascades soft-delete to related leads/notes per service logic) |
| POST | `/customers/:id/tags` | MANAGER+ | `{ tags: string[] }` — set tags (overwrite) |
| POST | `/customers/:id/merge` | ADMIN+ | `{ targetCustomerId }` — merge this customer into target; reassigns conversations, leads, appointments, notes; soft-deletes source |
| GET | `/customers/by-phone/:phone` | Any | lookup by E.164 phone |

### Business logic
- **Phone normalization**: every inbound phone is normalized to E.164 using `libphonenumber-js`, default country = tenant country. Reject if invalid. Strip the `whatsapp:` prefix when received from Twilio.
- **Dedup**: on auto-creation from WhatsApp (Feature 12), use `findUnique({ tenantId_phoneE164 })`; only create if missing.
- **Manual creation collision**: if phone already exists in tenant, return 409 with existing customer ID.
- **License plate**: stored uppercased, no dashes/spaces (e.g. `12ABC3`); validated against Dutch kenteken regex when `tenant.country === 'NL'`.
- **Merge**: atomic transaction; reassign FK rows from source to target, copy non-empty target-null fields from source, soft-delete source.
- **Consent**: setting `consentMarketing=true` requires also setting `consentGivenAt` (server timestamp), recorded in audit log.

### Validation
- `phoneE164`: must pass `libphonenumber-js` `isValidNumber`.
- `email`: optional RFC 5322.
- `tags`: max 20, each max 30 chars, alphanumeric + dash/underscore.
- `language`: must be in `tenant.settings.supportedLanguages`.

### Tests
- Dedup on phone within tenant.
- Same phone allowed in different tenants.
- Merge moves conversations/leads/appointments and preserves message ordering.
- Invalid phone rejected.

---

## Feature 8: Leads

### Purpose
A `Lead` is a sales opportunity for a customer (e.g. interested in a specific vehicle, requested a test drive, asked for a trade-in valuation). A customer can have multiple leads. Leads have status, source, assignee, and lifecycle timestamps.

### Data model
```prisma
model Lead {
  id            String     @id @default(cuid())
  tenantId      String
  tenant        Tenant     @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  customerId    String
  customer      Customer   @relation(fields: [customerId], references: [id], onDelete: Cascade)
  conversationId String?
  conversation  Conversation? @relation(fields: [conversationId], references: [id])

  type          LeadType                                       // SALES_INQUIRY | TEST_DRIVE | TRADE_IN | WORKSHOP_APK | WORKSHOP_MAINTENANCE | WORKSHOP_DAMAGE | OTHER
  status        LeadStatus @default(NEW)                       // NEW | CONTACTED | QUALIFIED | APPOINTMENT_BOOKED | WON | LOST | ARCHIVED
  source        LeadSource @default(WHATSAPP)                  // WHATSAPP | MANUAL | WEB | IMPORT
  priority      LeadPriority @default(MEDIUM)                  // LOW | MEDIUM | HIGH

  title         String                                          // short summary e.g. "Test drive — Opel Corsa 2019"
  description   String?    @db.Text

  // Structured payload (varies by type)
  payload       Json       @default("{}")                       // e.g. for TEST_DRIVE: { vehicleStockId, preferredDate, preferredTime }

  assignedToId  String?
  assignedTo    User?      @relation("AssignedTo", fields: [assignedToId], references: [id])

  qualifiedAt   DateTime?
  closedAt      DateTime?
  closeReason   String?                                         // for LOST: why
  estimatedValue Decimal?  @db.Decimal(10,2)
  currency      String     @default("EUR")

  notes         Note[]
  appointments  Appointment[]

  createdAt     DateTime   @default(now())
  updatedAt     DateTime   @updatedAt
  deletedAt     DateTime?

  @@index([tenantId, status])
  @@index([tenantId, type])
  @@index([tenantId, assignedToId])
  @@index([tenantId, createdAt])
  @@index([customerId])
}

enum LeadType {
  SALES_INQUIRY
  TEST_DRIVE
  TRADE_IN
  WORKSHOP_APK
  WORKSHOP_MAINTENANCE
  WORKSHOP_DAMAGE
  OTHER
}

enum LeadStatus { NEW CONTACTED QUALIFIED APPOINTMENT_BOOKED WON LOST ARCHIVED }
enum LeadSource { WHATSAPP MANUAL WEB IMPORT }
enum LeadPriority { LOW MEDIUM HIGH }
```

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/leads` | Any | list (filters: status, type, assignee, priority, search, dateRange) |
| POST | `/leads` | MANAGER+ | create manually |
| GET | `/leads/:id` | Any | detail incl. customer, conversation, appointments, notes |
| PATCH | `/leads/:id` | MANAGER+ | update fields (status, priority, assignee, payload, etc.) |
| POST | `/leads/:id/assign` | MANAGER+ | `{ userId }` |
| POST | `/leads/:id/status` | MANAGER+ | `{ status, closeReason? }` |
| DELETE | `/leads/:id` | ADMIN+ | soft delete |
| GET | `/leads/stats` | Any | counts by status, by type, average time-to-close |

### Business logic
- **Status transitions** are restricted:
  - `NEW → CONTACTED → QUALIFIED → APPOINTMENT_BOOKED → WON`
  - Any → `LOST` (requires `closeReason`)
  - Any → `ARCHIVED`
  - Reject illegal transitions with 409.
- **Side effects on transition**:
  - `NEW → CONTACTED`: nothing extra.
  - → `QUALIFIED`: set `qualifiedAt = now()`.
  - → `WON` or `LOST`: set `closedAt = now()`.
- **Auto-creation from WhatsApp** (used by AI engine, Feature 13): when AI classifies a message intent (TEST_DRIVE, TRADE_IN, WORKSHOP_*, SALES_INQUIRY), a Lead is created with `source=WHATSAPP`, `status=NEW`, `conversationId` set, `payload` populated from extracted entities.
- **Assignment**: if `notifyOnNewLead`, a `Notification` is created for the assignee (or all OWNER/ADMIN/MANAGER if unassigned).
- **Stats endpoint**: returns aggregates for the current tenant over a query-param date range (default last 30 days).

### Validation
- `title` max 200 chars; `description` max 5000 chars.
- `payload` validated against a per-`type` JSON schema (use ajv): e.g. `TEST_DRIVE` requires `vehicleStockId|vehicleDescription` and `preferredDate`.

### Tests
- Illegal status transition rejected.
- LOST requires `closeReason`.
- Stats endpoint returns correct counts on seed data.

---

## Feature 9: Conversations

### Purpose
A `Conversation` is one continuous WhatsApp thread between a customer and the dealership. There is typically one open conversation per customer at a time. Conversations carry state (AI vs human takeover), language, last message timestamp, unread count.

### Data model
```prisma
model Conversation {
  id            String   @id @default(cuid())
  tenantId      String
  tenant        Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  customerId    String
  customer      Customer @relation(fields: [customerId], references: [id], onDelete: Cascade)

  channel       ConversationChannel @default(WHATSAPP)
  status        ConversationStatus  @default(OPEN)
  mode          ConversationMode    @default(AI)               // AI | HUMAN
  language      String              @default("nl")

  // AI context
  aiSummary     String?             @db.Text                   // running summary, updated periodically
  aiTokenCount  Int                 @default(0)                // cumulative tokens used in this conversation
  aiTurnCount   Int                 @default(0)                // AI ↔ customer turn count (used for escalation)
  lastIntent    String?                                        // last classified intent

  takenOverById String?
  takenOverBy   User?              @relation("TakenOverBy", fields: [takenOverById], references: [id])
  takenOverAt   DateTime?

  closedAt      DateTime?
  closedReason  String?

  unreadCount   Int                @default(0)                  // unread by dealer staff

  lastMessageAt DateTime           @default(now())
  lastInboundAt DateTime?
  lastOutboundAt DateTime?

  leads         Lead[]
  messages      Message[]
  notes         Note[]

  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt

  @@index([tenantId, status, lastMessageAt(sort: Desc)])
  @@index([tenantId, customerId])
  @@index([tenantId, mode])
}

enum ConversationChannel { WHATSAPP }
enum ConversationStatus  { OPEN CLOSED }
enum ConversationMode    { AI HUMAN }
```
*(`User` model gets `takenOverConversations Conversation[] @relation("TakenOverBy")`)*

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/conversations` | Any | list (filters: status, mode, search by customer name/phone, assignee, hasUnread) |
| GET | `/conversations/:id` | Any | detail with customer + last N messages |
| GET | `/conversations/:id/messages` | Any | paginated message history (cursor-based, `before=messageId`) |
| POST | `/conversations/:id/takeover` | AGENT+ | switch `mode` to `HUMAN`, set `takenOverById`/`takenOverAt`. AI replies disabled. |
| POST | `/conversations/:id/release` | AGENT+ | switch `mode` back to `AI`, clear `takenOverBy*`. |
| POST | `/conversations/:id/close` | MANAGER+ | `{ reason? }` — set `status=CLOSED`, `closedAt`. |
| POST | `/conversations/:id/reopen` | MANAGER+ | reopen a closed conversation. |
| POST | `/conversations/:id/mark-read` | AGENT+ | set `unreadCount=0`. |
| POST | `/conversations/:id/send` | AGENT+ | `{ body, mediaUrl? }` — send a WhatsApp message as the dealer (delegates to Feature 12 sender). Allowed in both modes; sending in AI mode does not auto-takeover (configurable). |

### Business logic
- `unreadCount` increments on every inbound message; resets on `mark-read`.
- A customer with no open conversation gets a new one on the next inbound message; otherwise messages append to the open one.
- When a human takes over, the AI does not generate replies for that conversation until `release`.
- Closing a conversation prevents `send` (409); reopening allows it.

### Tests
- Takeover blocks AI processing in Feature 13.
- Closing locks send.
- Paginated message fetch returns oldest-first within a page; `before` cursor works.

---

## Feature 10: Messages

### Purpose
Individual WhatsApp messages within a conversation. Persist inbound and outbound, with delivery status tracking.

### Data model
```prisma
model Message {
  id              String        @id @default(cuid())
  tenantId        String
  tenant          Tenant        @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  conversationId  String
  conversation    Conversation  @relation(fields: [conversationId], references: [id], onDelete: Cascade)
  customerId      String                                            // denormalized for fast queries
  customer        Customer      @relation(fields: [customerId], references: [id], onDelete: Cascade)

  direction       MessageDirection                                  // INBOUND | OUTBOUND
  authorType      MessageAuthorType                                 // CUSTOMER | AI | HUMAN | SYSTEM
  authorUserId    String?                                           // for HUMAN
  authorUser      User?         @relation(fields: [authorUserId], references: [id])

  body            String?       @db.Text
  mediaUrl        String?                                           // outbound: our CDN url; inbound: Twilio media url (re-uploaded if persisted)
  mediaContentType String?
  mediaPersistedAt DateTime?                                        // when we downloaded and stored a copy

  providerMessageSid String?    @unique                             // Twilio SID
  providerStatus  MessageProviderStatus?                            // QUEUED | SENT | DELIVERED | READ | FAILED | UNDELIVERED
  providerErrorCode String?
  providerErrorMessage String?

  intent          String?                                           // AI-classified intent
  aiPromptTokens  Int?
  aiCompletionTokens Int?
  aiModel         String?
  aiTemperature   Float?

  inReplyToId     String?
  inReplyTo       Message?      @relation("InReplyTo", fields: [inReplyToId], references: [id])
  replies         Message[]     @relation("InReplyTo")

  sentAt          DateTime?
  deliveredAt     DateTime?
  readAt          DateTime?
  failedAt        DateTime?

  createdAt       DateTime      @default(now())

  @@index([tenantId, conversationId, createdAt(sort: Desc)])
  @@index([providerMessageSid])
  @@index([tenantId, customerId])
}

enum MessageDirection { INBOUND OUTBOUND }
enum MessageAuthorType { CUSTOMER AI HUMAN SYSTEM }
enum MessageProviderStatus { QUEUED SENT DELIVERED READ FAILED UNDELIVERED }
```

### API endpoints
- Messages are not directly created via REST (creation paths: inbound webhook in Feature 12, outbound send in Features 9/12). Exposed via:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/messages/:id` | Any | message detail |
| GET | `/conversations/:id/messages` | Any | (covered in Feature 9) |
| POST | `/messages/:id/retry` | MANAGER+ | re-attempt sending a FAILED outbound message |

### Business logic
- Maximum body length 4096 chars (WhatsApp limit).
- `mediaUrl` for outbound must be HTTPS and publicly fetchable by Twilio.
- For inbound media, a background job downloads from Twilio (with Twilio Basic Auth), uploads to object storage, sets `mediaUrl` to our URL, sets `mediaPersistedAt`. Until then, the Twilio URL is stored.
- `inReplyTo` is set when applicable (outbound AI/HUMAN reply referencing the inbound that triggered it).

### Tests
- Outbound message persisted with provider SID; status webhook updates it.
- Inbound media URL replaced after persistence job runs.

---

## Feature 11: Internal Notes

### Purpose
Internal notes attached to a Customer, Lead, or Conversation. Visible only to dealer staff. Mentions notify mentioned users.

### Data model
```prisma
model Note {
  id            String   @id @default(cuid())
  tenantId      String
  tenant        Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  customerId    String?
  customer      Customer? @relation(fields: [customerId], references: [id], onDelete: Cascade)
  leadId        String?
  lead          Lead?    @relation(fields: [leadId], references: [id], onDelete: Cascade)
  conversationId String?
  conversation  Conversation? @relation(fields: [conversationId], references: [id], onDelete: Cascade)

  body          String   @db.Text
  mentions      String[] @default([])                            // user IDs mentioned

  authorId      String
  author        User     @relation(fields: [authorId], references: [id])

  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt
  deletedAt     DateTime?

  @@index([tenantId, customerId])
  @@index([tenantId, leadId])
  @@index([tenantId, conversationId])
}
```

### API endpoints

| Method | Path | Auth | Body |
|---|---|---|---|
| POST | `/notes` | Any | `{ body, customerId?, leadId?, conversationId?, mentions?: string[] }` (exactly one of the parent IDs required) |
| GET | `/notes` | Any | filter by parent (`?customerId=` or `?leadId=` or `?conversationId=`) |
| PATCH | `/notes/:id` | Author only | `{ body, mentions? }` |
| DELETE | `/notes/:id` | Author or ADMIN+ | soft delete |

### Business logic
- Exactly one of `customerId | leadId | conversationId` must be set.
- `mentions` must reference users in the same tenant; each mention generates a `Notification` (type `NOTE_MENTION`).
- `body` max 5000 chars; supports markdown (server stores raw).

---

## Feature 12: WhatsApp / Twilio Integration

### Purpose
Send and receive WhatsApp messages via the Twilio WhatsApp Business API. Handles inbound webhooks, outbound sending, status callbacks, media handling, and per-tenant credentials.

### Data model
```prisma
model WhatsAppConfig {
  id              String   @id @default(cuid())
  tenantId        String   @unique
  tenant          Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  provider        String   @default("twilio")                  // future-proof
  twilioAccountSid String?                                     // encrypted
  twilioAuthToken  String?                                     // encrypted
  whatsappNumber   String                                      // E.164, e.g. "+31201234567" — the sender
  messagingServiceSid String?                                  // optional

  useSharedCredentials Boolean @default(true)                  // if true, use platform Twilio creds; else use per-tenant
  webhookVerified  Boolean  @default(false)                    // set true after first valid signed webhook

  createdAt        DateTime @default(now())
  updatedAt        DateTime @updatedAt
}

model WebhookEvent {
  id          String   @id @default(cuid())
  source      String                                           // "twilio" | "stripe" | "calendly" | "google"
  externalId  String                                           // provider event ID for idempotency
  payload     Json
  receivedAt  DateTime @default(now())
  processedAt DateTime?
  error       String?

  @@unique([source, externalId])
  @@index([source, receivedAt])
}
```

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/webhooks/twilio/inbound` | Twilio signature | inbound WhatsApp messages |
| POST | `/webhooks/twilio/status` | Twilio signature | outbound message delivery status callbacks |
| GET | `/whatsapp/config` | OWNER/ADMIN | get current config (auth token redacted) |
| PUT | `/whatsapp/config` | OWNER/ADMIN | update — `{ whatsappNumber, useSharedCredentials, twilioAccountSid?, twilioAuthToken? }` |
| POST | `/whatsapp/test` | OWNER/ADMIN | `{ toPhone, body }` — send a test message to verify config |

### Twilio webhook signature validation (mandatory)
- Implement a `TwilioSignatureGuard` that:
  - Reconstructs the signed string per Twilio docs (URL + sorted params for `application/x-www-form-urlencoded`; URL + raw body for JSON).
  - Computes HMAC-SHA1 with the configured Twilio auth token (per-tenant if configured, else platform).
  - Compares to `X-Twilio-Signature` header in constant time.
  - Returns 403 if invalid.
- Tenant resolution from webhook: Twilio inbound payload includes `To` (the dealer's WhatsApp number). Look up `WhatsAppConfig` by `whatsappNumber` to determine tenant. If none found, return 404 (do not 500).

### Inbound message processing (webhook → DB → AI)
1. Validate signature.
2. Idempotency: insert into `WebhookEvent` with `(source='twilio', externalId=MessageSid)`. If duplicate, return 200 immediately.
3. Resolve tenant by `To` number.
4. Normalize sender phone (`From` → strip `whatsapp:`, ensure E.164).
5. Upsert `Customer` (find by `phoneE164` or create with `source=WHATSAPP`); update `lastSeenAt`.
6. Find or create the customer's currently OPEN `Conversation`. If none, create with `mode=AI`, language from customer/tenant default.
7. Insert `Message` (direction=INBOUND, authorType=CUSTOMER, body, mediaUrl, providerMessageSid).
8. Update conversation `lastMessageAt`, `lastInboundAt`, increment `unreadCount`.
9. Enqueue jobs:
   - `media-persist` if media present.
   - `ai-respond` if `conversation.mode === AI` AND `tenant.settings.aiEnabled` AND auto-reply policy allows (see Feature 14).
10. Respond 200 with empty TwiML (`<Response/>`).

### Outbound sending
- Service `WhatsAppService.send(tenantId, { to, body, mediaUrl?, conversationId, inReplyToId?, authorType, authorUserId? })`:
  - Resolves the right Twilio creds (per-tenant or shared).
  - Calls Twilio Messages API; on success persists `Message` with status `QUEUED` (or `SENT` based on response) and `providerMessageSid`.
  - Updates conversation `lastOutboundAt`, `lastMessageAt`.
  - On HTTP error, persists message with `providerStatus=FAILED` and error fields, and creates an internal notification.
  - Records usage (Feature 19).
- Retries: 3 attempts with exponential backoff on 5xx/429 only; never retry on 4xx (except 429).

### Status callback
- Validate signature, idempotent on `MessageSid + MessageStatus`.
- Update the corresponding `Message`: `providerStatus`, `deliveredAt`/`readAt`/`failedAt`, `providerErrorCode/Message`.

### Media handling
- Inbound: `media-persist` job downloads each `MediaUrl` with Twilio Basic Auth, stores in S3/DO Spaces under `tenants/{tenantId}/messages/{messageId}/{idx}.{ext}`, sets `Message.mediaUrl` to the public CDN URL, sets `mediaPersistedAt`.
- Max media size 16MB (WhatsApp).
- Allowed types: `image/jpeg`, `image/png`, `image/webp`, `application/pdf`, `audio/ogg`, `audio/mpeg`, `video/mp4`. Reject others (log + skip).

### Acceptance criteria
- Invalid Twilio signature → 403, no DB writes.
- Duplicate webhook → 200, no duplicate Message rows.
- Unknown `To` number → 404, logged for investigation.
- Outbound 5xx → message marked FAILED only after final retry; status updated by callback when eventually delivered.

### Tests
- Signature validation pass/fail.
- Inbound creates Customer+Conversation+Message.
- Idempotency on `WebhookEvent`.
- Status callback updates message fields.
- Media persist job replaces URL.

---

## Feature 13: AI Conversation Engine

### Purpose
Generate AI replies to inbound WhatsApp messages using OpenAI, with per-conversation context, intent classification, function-calling for structured outcomes (create lead, book appointment, escalate, request RDW lookup), and graceful fallback.

### Architecture
- Triggered by `ai-respond` job (enqueued by inbound webhook).
- The job:
  1. Re-reads the conversation, ensures `mode === AI`, status `OPEN`, `aiEnabled`, no human takeover.
  2. Checks auto-reply policy (Feature 14): business hours, rate limits, suppression rules.
  3. Builds the prompt (see below).
  4. Calls OpenAI chat completions with function calling enabled.
  5. Handles the response:
     - If text reply → send via WhatsAppService.
     - If function call → execute the function, then loop with the function result appended (max 3 function-call rounds to bound cost).
  6. Updates conversation: `aiTurnCount++`, `aiTokenCount += usage.total`, `lastIntent`.
  7. If `aiTurnCount > settings.aiMaxTurnsBeforeEscalation` and intent unresolved → trigger escalation (Feature 15).
  8. Records usage (Feature 19).

### Prompt architecture
- **System prompt** (assembled per call):
  - Base instructions (role: customer-service assistant for a car dealership; never claim to be human; always respond in the customer's language; keep replies under 700 chars; do not invent prices/availability).
  - Dealer-specific persona from `settings.aiPersona`.
  - Business context: dealer name, business hours (today's window in local time), supported services (workshop APK/maintenance/damage, sales, test drive, trade-in), escalation conditions.
  - Tone instruction from `settings.aiTone`.
  - Language instruction (current `conversation.language`).
  - Available tools (function definitions, see below).
  - Out-of-hours behavior if outside business hours.
- **Messages**:
  - Last `aiSummary` (if exists) as a `system` message: "Conversation so far: ..."
  - Last N raw messages (default 20, env-configurable), oldest → newest, mapping CUSTOMER → `user`, AI/HUMAN/SYSTEM → `assistant`.
  - Current inbound message as `user`.
- After every K turns (default 10), enqueue `ai-summarize` job that rewrites `aiSummary` and drops detail.

### Function/tool definitions (provided to OpenAI)
- `classify_intent(intent: enum, confidence: number)` — REQUIRED on first turn; intents: `GREETING | WORKSHOP_APK | WORKSHOP_MAINTENANCE | WORKSHOP_DAMAGE | SALES_INQUIRY | TEST_DRIVE | TRADE_IN | VEHICLE_INQUIRY | APPOINTMENT_RESCHEDULE | APPOINTMENT_CANCEL | COMPLAINT | OTHER | SMALL_TALK`.
- `create_lead(type, title, payload)` — creates a Lead.
- `lookup_vehicle_by_plate(plate)` — calls RDW (Feature 22), returns vehicle info.
- `propose_appointment_slots(type, preferredDateRange)` — returns 3–5 candidate slots from Google Calendar (Feature 24).
- `book_appointment(slotIso, type, vehicleInfo?, notes?)` — creates Appointment.
- `request_human(reason)` — escalate.
- `set_customer_language(language)` — when detected language differs from current.
- `set_customer_field(field, value)` — update Customer (firstName, lastName, email, licensePlate).

### Multi-language
- On every inbound, run a lightweight language detect (use OpenAI's structured output or `franc` library). If detected ∈ `tenant.settings.supportedLanguages` and differs from `conversation.language`, the model can call `set_customer_language` and the next reply is in that language.

### Human takeover & fallback
- If `conversation.mode === HUMAN` when job runs → skip (the human is handling it).
- If OpenAI returns an error → retry once with `OPENAI_MODEL_FALLBACK`. If that also fails, send the `outOfHoursReply` or a generic "We'll get back to you shortly" message, set `lastIntent = 'AI_ERROR'`, and create an escalation notification.
- If `aiTurnCount > aiMaxTurnsBeforeEscalation` and intent is still `OTHER`/unresolved → call `request_human` and stop.

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/ai/respond` | INTERNAL (HMAC-signed by n8n) or MANAGER+ | manually trigger a response generation for a conversation (debugging / reprocessing) |
| POST | `/ai/preview` | MANAGER+ | `{ conversationId, draftPrompt? }` — returns the would-be reply without sending (for testing prompts) |
| GET | `/ai/usage` | OWNER/ADMIN | usage stats for current tenant (tokens, messages) for date range |

### Acceptance criteria
- Reply latency p95 < 5s under normal load (excluding OpenAI tail latency).
- Token usage recorded on every call.
- No reply is sent if `mode=HUMAN` or `status=CLOSED`.
- Function-call loop hard-capped at 3 rounds per inbound to prevent infinite tool loops.
- Replies always respect the configured language.

### Tests
- Unit: prompt builder produces deterministic prompt given fixed inputs.
- Integration: stub OpenAI; assert lead creation tool call results in DB row.
- Integration: takeover during job causes skip.
- Integration: turn cap triggers escalation.

---

## Feature 14: Automation Rules (Business hours, Auto-reply, Suppression)

### Purpose
Per-tenant policies that govern whether/how the AI replies, including business-hours behavior, keyword shortcuts, and per-conversation throttling.

### Data model
```prisma
model AutoReplyRule {
  id           String   @id @default(cuid())
  tenantId     String
  tenant       Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  name         String
  enabled      Boolean  @default(true)
  priority     Int      @default(100)                       // lower runs first

  trigger      Json                                          // { type: "KEYWORD"|"INTENT"|"OUT_OF_HOURS"|"FIRST_MESSAGE", value?: string[] }
  action       Json                                          // { type: "REPLY_TEMPLATE"|"SKIP_AI"|"TAG_CUSTOMER", value: ... }

  language     String?                                       // null = all languages
  validFrom    DateTime?
  validUntil   DateTime?

  createdAt    DateTime @default(now())
  updatedAt    DateTime @updatedAt

  @@index([tenantId, enabled, priority])
}

model EscalationRule {
  id          String   @id @default(cuid())
  tenantId    String
  tenant      Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  name        String
  enabled     Boolean  @default(true)
  condition   Json                                            // { type: "INTENT"|"KEYWORD"|"SENTIMENT"|"TURN_COUNT", value: ... }
  action      Json                                            // { type: "NOTIFY"|"ASSIGN_TO_USER"|"REQUEST_HUMAN", targetUserId?: ... }

  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt

  @@index([tenantId, enabled])
}
```

### Business logic
- **Business hours check**: at the moment an inbound triggers `ai-respond`, compute "is now within `settings.businessHours` in `settings.timezone`, excluding `holidays`". If outside:
  - If a rule with `trigger.type === "OUT_OF_HOURS"` exists → apply its action (likely send `outOfHoursReply` template once per conversation per off-hours block, suppress further AI replies until next open block).
  - Else: default behavior is to still let the AI reply but prepend an out-of-hours notice.
- **Keyword rules**: case-insensitive match against the inbound body. Examples: keyword `"stop"` → action `SKIP_AI` + tag customer `do_not_contact` (consent withdrawal).
- **Intent rules**: matched after intent classification step.
- **Action `REPLY_TEMPLATE`**: send a fixed template (supports `{{customer.firstName}}`, `{{dealer.name}}`, `{{date}}` substitutions) and skip AI for this turn.
- **Action `SKIP_AI`**: do not reply.
- **Throttling**: hard cap of one outbound message per 2 seconds per conversation (queue and serialize). Reject > 10 AI replies per conversation per hour (escalate instead).

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/automation/auto-reply-rules` | MANAGER+ | list |
| POST | `/automation/auto-reply-rules` | ADMIN+ | create |
| PATCH | `/automation/auto-reply-rules/:id` | ADMIN+ | update |
| DELETE | `/automation/auto-reply-rules/:id` | ADMIN+ | delete |
| GET | `/automation/escalation-rules` | MANAGER+ | list |
| POST | `/automation/escalation-rules` | ADMIN+ | create |
| PATCH | `/automation/escalation-rules/:id` | ADMIN+ | update |
| DELETE | `/automation/escalation-rules/:id` | ADMIN+ | delete |

### Validation
- `trigger.type` and `action.type` enums enforced.
- Templates max 1000 chars.

### Tests
- Out-of-hours response delivered exactly once per off-hours block per conversation.
- Keyword `stop` tags customer and suppresses AI.
- Turn-count escalation triggers notification.

---

## Feature 15: Notifications

### Purpose
In-app notifications for dealer users (new lead, escalation, mention, appointment booked, message failed, billing event). Polled by the dashboard; optionally pushed via WebSocket later.

### Data model
```prisma
model Notification {
  id          String   @id @default(cuid())
  tenantId    String
  tenant      Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  userId      String?                                          // null = broadcast to OWNER/ADMIN/MANAGER
  user        User?    @relation(fields: [userId], references: [id])

  type        NotificationType
  title       String
  body        String?
  data        Json?                                            // { leadId, conversationId, ... }
  readAt      DateTime?

  createdAt   DateTime @default(now())

  @@index([tenantId, userId, readAt])
  @@index([tenantId, createdAt(sort: Desc)])
}

enum NotificationType {
  NEW_LEAD
  LEAD_ASSIGNED
  ESCALATION
  NOTE_MENTION
  APPOINTMENT_BOOKED
  APPOINTMENT_REMINDER
  APPOINTMENT_CANCELED
  MESSAGE_FAILED
  AI_ERROR
  BILLING_PAYMENT_FAILED
  BILLING_SUBSCRIPTION_UPDATED
  SYSTEM
}
```

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/notifications` | Any | paginated; `?unread=true` filter |
| GET | `/notifications/unread-count` | Any | `{ count }` |
| POST | `/notifications/:id/read` | Any | mark read |
| POST | `/notifications/read-all` | Any | mark all my notifications read |

### Business logic
- Broadcast notifications (`userId=null`) are visible to all OWNER/ADMIN/MANAGER users; `readAt` per user is tracked in a join table `NotificationRead { notificationId, userId, readAt }` (add this model).
- Side-effect: also send an email or WhatsApp ping when `settings.escalationEmail/Phone` is set AND `type ∈ {ESCALATION, BILLING_PAYMENT_FAILED}`.

### Tests
- Broadcast notification appears for all eligible users, read state per-user.
- Mark-all-read only affects the calling user.


# PART C — APPOINTMENTS, WORKSHOP & SALES FLOWS

---

## Feature 16: Appointments

### Purpose
Schedule, track, remind, and update appointments (workshop services and sales activities like test drives) for customers.

### Data model
```prisma
model Appointment {
  id              String   @id @default(cuid())
  tenantId        String
  tenant          Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  customerId      String
  customer        Customer @relation(fields: [customerId], references: [id], onDelete: Cascade)
  leadId          String?
  lead            Lead?    @relation(fields: [leadId], references: [id])
  createdByUserId String?
  createdByUser   User?    @relation("AppointmentCreatedBy", fields: [createdByUserId], references: [id])

  type            AppointmentType
  status          AppointmentStatus @default(SCHEDULED)
  source          AppointmentSource @default(AI)

  startAt         DateTime
  endAt           DateTime
  timezone        String   @default("Europe/Amsterdam")
  location        String?                                       // e.g. "Example Motors, Main Street 12"

  title           String
  description     String?  @db.Text
  vehicleInfo     Json?                                         // { plate, make, model, year, vin }

  // External calendar sync
  googleEventId   String?
  calendlyEventUri String?

  // Reminders
  remindersSent   Json     @default("[]")                      // [{ type: "T-24H"|"T-2H", at: iso }]

  cancelledAt     DateTime?
  cancelReason    String?
  rescheduledFromId String?
  rescheduledFrom Appointment? @relation("Reschedule", fields: [rescheduledFromId], references: [id])
  reschedules     Appointment[] @relation("Reschedule")

  createdAt       DateTime @default(now())
  updatedAt       DateTime @updatedAt

  @@index([tenantId, startAt])
  @@index([tenantId, status, startAt])
  @@index([tenantId, customerId])
  @@index([leadId])
}

enum AppointmentType {
  APK
  MAINTENANCE
  DAMAGE_REPAIR
  TEST_DRIVE
  TRADE_IN_INSPECTION
  SALES_CONSULTATION
  OTHER
}

enum AppointmentStatus { SCHEDULED CONFIRMED COMPLETED NO_SHOW CANCELLED RESCHEDULED }
enum AppointmentSource { AI MANUAL CALENDLY GOOGLE WEB }
```
*(`User` model gets `createdAppointments Appointment[] @relation("AppointmentCreatedBy")`)*

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/appointments` | Any | list (filters: status, type, date range, customer, assignee) |
| POST | `/appointments` | MANAGER+ or AI service | create |
| GET | `/appointments/:id` | Any | detail |
| PATCH | `/appointments/:id` | MANAGER+ | update fields |
| POST | `/appointments/:id/confirm` | MANAGER+ | set status CONFIRMED, send WhatsApp confirmation to customer |
| POST | `/appointments/:id/cancel` | MANAGER+ or customer-via-AI | `{ reason }` — set CANCELLED, send WhatsApp cancellation, remove from Google Calendar |
| POST | `/appointments/:id/reschedule` | MANAGER+ or customer-via-AI | `{ startAt, endAt }` — creates new Appointment with `rescheduledFromId`, marks old as RESCHEDULED |
| POST | `/appointments/:id/complete` | MANAGER+ | set COMPLETED |
| POST | `/appointments/:id/no-show` | MANAGER+ | set NO_SHOW |
| GET | `/appointments/availability` | MANAGER+ or AI service | `?type=APK&from=...&to=...` — returns available slots from Google Calendar |

### Business logic
- `endAt > startAt` and overlap detection within tenant (warn but allow; do not hard-reject — multiple bays exist).
- On create:
  - Push to Google Calendar (if connection exists, Feature 24); store `googleEventId`.
  - Send WhatsApp confirmation to customer (templated, localized).
  - Create `Notification` for OWNER/ADMIN/MANAGER if `notifyOnNewAppointment`.
  - Schedule reminder jobs (T-24h, T-2h) — see below.
  - If linked Lead, transition Lead status to `APPOINTMENT_BOOKED`.
- On cancel: delete Google event; send WhatsApp cancellation; cancel reminder jobs; if linked Lead, do not auto-change Lead status (manual).
- On reschedule: clone with new times; cancel old in Google; create new in Google; send WhatsApp; recompute reminders.
- **Reminders**: BullMQ delayed jobs `appointment-reminder-24h`, `appointment-reminder-2h`. Each:
  - Re-fetches appointment; if status ∈ {CANCELLED, RESCHEDULED, COMPLETED, NO_SHOW}, skip.
  - Sends WhatsApp message (localized template).
  - Appends to `remindersSent`.

### Validation
- `startAt` must be in future on create.
- `vehicleInfo.plate` validated as Dutch kenteken when applicable.

### Tests
- Create syncs to Google Calendar (stub).
- Cancel removes Google event.
- Reminder job skipped for cancelled appointment.
- Reschedule chain navigable via `rescheduledFromId`.

---

## Feature 17: Workshop Flows (APK, Maintenance, Damage)

### Purpose
Service endpoints that wrap appointment creation with workshop-specific defaults, payloads, and WhatsApp confirmation templates. These are the surfaces called by the AI engine via `book_appointment` and also exposed for the dashboard.

### Endpoints

| Method | Path | Auth | Body |
|---|---|---|---|
| POST | `/workshop/apk` | MANAGER+ or AI | `{ customerId, plate, preferredAt, notes? }` |
| POST | `/workshop/maintenance` | MANAGER+ or AI | `{ customerId, plate, serviceType: enum(OIL_CHANGE\|TIRES\|BRAKES\|GENERAL\|OTHER), preferredAt, notes? }` |
| POST | `/workshop/damage` | MANAGER+ or AI | `{ customerId, plate?, damageDescription, photos: string[], preferredAt?, notes? }` |

### Business logic
- For APK: default duration 60 min. Auto-populate `vehicleInfo` by calling RDW (Feature 22) using the plate; cache result on Customer.
- For Maintenance: duration depends on `serviceType` (OIL_CHANGE 45m, TIRES 60m, BRAKES 90m, GENERAL 120m, OTHER 60m).
- For Damage: duration 30 min initial assessment; description required, photos optional (URLs from previously persisted WhatsApp media or uploaded by dealer).
- Each endpoint:
  - Creates Lead with `type=WORKSHOP_*`, `status=APPOINTMENT_BOOKED`, payload includes service details.
  - Creates Appointment via Feature 16.
  - Sends WhatsApp confirmation using a workshop-specific template (localized; falls back to default if dealer didn't customize).
  - Creates Notification.

### Tests
- APK creates Lead + Appointment + Google event + WhatsApp confirmation (stubbed).
- Damage without preferredAt creates Lead in NEW status without Appointment, requesting dealer follow-up.

---

## Feature 18: Sales Flows (Test Drive, Trade-in, Vehicle Inquiry)

### Purpose
Wrappers for the sales side, used by the AI and dashboard.

### Endpoints

| Method | Path | Auth | Body |
|---|---|---|---|
| POST | `/sales/test-drive` | MANAGER+ or AI | `{ customerId, vehicleStockId?, vehicleDescription, preferredAt, notes? }` |
| POST | `/sales/trade-in` | MANAGER+ or AI | `{ customerId, plate?, make, model, year, mileage, condition: enum(EXCELLENT\|GOOD\|FAIR\|POOR), photos?: string[], preferredAt?, notes? }` |
| POST | `/sales/vehicle-inquiry` | MANAGER+ or AI | `{ customerId, vehicleStockId?, vehicleDescription, question, preferredContact: enum(WHATSAPP\|CALL\|EMAIL) }` |

### Business logic
- **Test drive**: creates Lead `type=TEST_DRIVE`, payload includes vehicle and preferences. Creates Appointment `type=TEST_DRIVE` (60 min) when `preferredAt` set; otherwise Lead remains NEW for dealer follow-up.
- **Trade-in**: creates Lead `type=TRADE_IN`. If `preferredAt` set, creates Appointment `type=TRADE_IN_INSPECTION` (30 min). Vehicle data enriched via RDW if `plate` provided.
- **Vehicle inquiry**: creates Lead `type=SALES_INQUIRY` only — no appointment. Notifies sales staff.

### Tests
- Test drive without preferredAt creates Lead only.
- Trade-in plate enriches vehicle fields from RDW (stub).

---

# PART D — INTEGRATIONS

---

## Feature 19: Usage Tracking

### Purpose
Meter per-tenant usage for billing visibility and plan enforcement: messages sent/received, AI tokens, AI calls, appointments created.

### Data model
```prisma
model UsageRecord {
  id          String   @id @default(cuid())
  tenantId    String
  tenant      Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  periodStart DateTime                                       // start of UTC month
  periodEnd   DateTime                                       // exclusive end

  messagesIn  Int      @default(0)
  messagesOut Int      @default(0)
  aiCalls     Int      @default(0)
  aiPromptTokens   Int @default(0)
  aiCompletionTokens Int @default(0)
  appointmentsCreated Int @default(0)
  leadsCreated Int     @default(0)

  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt

  @@unique([tenantId, periodStart])
  @@index([tenantId])
}
```

### Mechanism
- `UsageService.increment(tenantId, field, delta=1)` upserts the current month's record and increments atomically. Called from:
  - Inbound message handler → `messagesIn += 1`
  - Outbound send → `messagesOut += 1`
  - AI engine after each OpenAI call → `aiCalls += 1`, token fields += usage
  - Appointment create → `appointmentsCreated += 1`
  - Lead create → `leadsCreated += 1`
- Period boundary: UTC month. A scheduled job at month rollover creates next month's row lazily on first write.

### Plan enforcement (consumed in Feature 20)
- Hard limits per plan (e.g. Starter = 1000 messagesOut/month). When 100% reached:
  - For `messagesOut`: AI replies are skipped; inbound persisted normally; create a `Notification` type `BILLING_PAYMENT_FAILED`-like for usage limit.
  - For appointments: do not block (warn only).
- Soft alerts at 80% and 100% via Notification.

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/usage/current` | OWNER/ADMIN | current month usage + plan limits + percentage |
| GET | `/usage/history` | OWNER/ADMIN | last 12 months |

### Tests
- Atomic increment under concurrent writes (use Prisma `update { ... increment }`).
- Period rollover creates new row.
- 100% limit blocks outbound AI replies.

---

## Feature 20: Stripe Billing

### Purpose
Subscription billing via Stripe Checkout, with webhook-driven state, invoices, and plan enforcement.

### Data model
```prisma
model Plan {
  id          String   @id @default(cuid())
  code        String   @unique                                // "starter" | "pro" | "enterprise"
  name        String
  stripePriceId String @unique
  monthlyPriceCents Int
  currency    String   @default("EUR")
  features    Json                                            // { messagesOutLimit: 1000, includesAdvancedAnalytics: false, ... }
  active      Boolean  @default(true)
  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt
}

model Subscription {
  id          String   @id @default(cuid())
  tenantId    String   @unique
  tenant      Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  planId      String?
  plan        Plan?    @relation(fields: [planId], references: [id])

  stripeCustomerId String  @unique
  stripeSubscriptionId String? @unique

  status      SubscriptionStatus                              // TRIALING ACTIVE PAST_DUE CANCELED INCOMPLETE INCOMPLETE_EXPIRED UNPAID PAUSED
  currentPeriodStart DateTime?
  currentPeriodEnd   DateTime?
  cancelAtPeriodEnd  Boolean @default(false)
  canceledAt         DateTime?
  trialEndsAt        DateTime?

  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt
}

enum SubscriptionStatus { TRIALING ACTIVE PAST_DUE CANCELED INCOMPLETE INCOMPLETE_EXPIRED UNPAID PAUSED }

model Invoice {
  id              String   @id @default(cuid())
  tenantId        String
  tenant          Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  stripeInvoiceId String   @unique
  number          String?
  status          String                                       // draft|open|paid|void|uncollectible
  amountDue       Int                                          // cents
  amountPaid      Int
  currency        String
  hostedInvoiceUrl String?
  pdfUrl          String?
  periodStart     DateTime?
  periodEnd       DateTime?
  paidAt          DateTime?

  createdAt       DateTime @default(now())
  updatedAt       DateTime @updatedAt

  @@index([tenantId])
}
```

### API endpoints

| Method | Path | Auth | Body |
|---|---|---|---|
| GET | `/billing/plans` | Any (authenticated) | list active plans |
| GET | `/billing/subscription` | OWNER | current subscription with plan + limits + usage |
| POST | `/billing/checkout-session` | OWNER | `{ planCode, successUrl, cancelUrl }` → `{ url }` Stripe Checkout |
| POST | `/billing/customer-portal` | OWNER | `{ returnUrl }` → `{ url }` Stripe Billing Portal |
| GET | `/billing/invoices` | OWNER | paginated |
| GET | `/billing/invoices/:id` | OWNER | detail |
| POST | `/webhooks/stripe` | Stripe signature | webhook receiver |

### Webhook handling
- Verify signature with `STRIPE_WEBHOOK_SECRET`.
- Idempotent via `WebhookEvent (source='stripe', externalId=event.id)`.
- Handle these events:
  - `customer.subscription.created`, `.updated`, `.deleted`: sync `Subscription` row (status, period, planId from price ID, cancelAtPeriodEnd).
  - `invoice.created`, `.finalized`, `.paid`, `.payment_failed`, `.voided`: upsert Invoice; on `payment_failed` create `BILLING_PAYMENT_FAILED` notification, and after 7 days past due (cron) suspend tenant (`Tenant.status = SUSPENDED`).
  - `customer.subscription.trial_will_end`: notify OWNER 3 days before trial end.
  - `checkout.session.completed`: link `stripeCustomerId` to tenant if not already linked; the actual subscription details come via the `subscription.*` events.

### Business logic
- On register, do **not** create a Stripe customer yet. Create lazily on first `checkout-session` request.
- `Tenant.trialEndsAt` is set at signup. While in trial and no Subscription exists, tenant is fully functional. Once trial ends with no subscription → set `Tenant.status = SUSPENDED` (cron at midnight UTC daily).
- Plan limits read from `Plan.features` JSON; usage enforcement in Feature 19 reads via `tenant.subscription.plan`.
- The `OWNER` user's email is the Stripe customer email.

### Tests
- Stripe webhook signature failure → 400, no DB write.
- Idempotent: same event ID processed twice updates once.
- Trial expiry cron suspends tenants with no subscription.
- Past-due invoice triggers payment failed notification.

---

## Feature 21: Admin Dashboard Backend

### Purpose
Endpoints for platform admins to manage tenants, users, plans, feature flags, usage, and system analytics.

### Data model
```prisma
model AdminUser {
  id            String   @id @default(cuid())
  email         String   @unique
  passwordHash  String
  firstName     String
  lastName      String
  role          AdminRole @default(ADMIN)
  status        UserStatus @default(ACTIVE)
  lastLoginAt   DateTime?
  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt

  auditLogs     AuditLog[]
}

enum AdminRole { SUPERADMIN ADMIN SUPPORT }

model FeatureFlag {
  id          String   @id @default(cuid())
  tenantId    String?                                          // null = global default
  key         String                                           // e.g. "advanced_analytics"
  enabled     Boolean
  notes       String?
  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt
  tenant      Tenant?  @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  @@unique([tenantId, key])
  @@index([key])
}
```

### Admin authentication
- Separate JWT scope (`scope: 'admin'`), separate secret derivation (use `JWT_ACCESS_SECRET` + a fixed admin salt to avoid token cross-use).
- Routes mounted under `/admin/*`, gated by `AdminAuthGuard` (checks scope=admin).
- Admin login endpoint: `POST /admin/auth/login`. No registration endpoint — admins are seeded via a CLI command `npm run admin:create -- --email=... --password=...`.
- All admin requests bypass tenant scoping via `PrismaService.asAdmin()`.

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/admin/auth/login` | login |
| POST | `/admin/auth/refresh` | refresh |
| POST | `/admin/auth/logout` | logout |
| GET | `/admin/me` | profile |
| GET | `/admin/tenants` | list (paginated, search, filter status/plan) |
| GET | `/admin/tenants/:id` | detail (users, subscription, usage, recent activity) |
| PATCH | `/admin/tenants/:id` | update name, status, country, etc. |
| POST | `/admin/tenants/:id/suspend` | `{ reason }` |
| POST | `/admin/tenants/:id/reactivate` | – |
| DELETE | `/admin/tenants/:id` | soft delete |
| POST | `/admin/tenants/:id/impersonate` | issue a short-lived (15-min, non-refreshable) dealer-scoped JWT for that tenant's OWNER, logged in audit |
| GET | `/admin/users` | cross-tenant user search |
| PATCH | `/admin/users/:id/status` | suspend/reactivate user |
| POST | `/admin/users/:id/reset-password` | force password reset email |
| GET | `/admin/plans` | list |
| POST | `/admin/plans` | create (mirrors a Stripe price) |
| PATCH | `/admin/plans/:id` | update features/limits/active |
| GET | `/admin/feature-flags` | list (global + per-tenant) |
| POST | `/admin/feature-flags` | upsert |
| GET | `/admin/usage` | aggregated usage across tenants (filter by date range, plan) |
| GET | `/admin/analytics/overview` | counts: tenants by status, MRR, messages this month, AI tokens this month, top tenants by usage |
| GET | `/admin/audit-logs` | paginated, filter by tenant/user/action/dateRange |
| GET | `/admin/webhook-events` | inspect inbound webhook history (filter source, status) |
| POST | `/admin/webhook-events/:id/replay` | replay a webhook payload through the original handler |

### Feature flag resolution
- `FeatureFlagService.isEnabled(tenantId, key)`:
  1. Look up tenant-specific override `(tenantId, key)`. If found, return its `enabled`.
  2. Look up global default `(tenantId=null, key)`. If found, return its `enabled`.
  3. Default false.

### Tests
- Impersonation token cannot be refreshed.
- Tenant scoping bypass only available within `asAdmin` context.
- Suspending tenant blocks dealer login.
- Feature flag precedence (tenant > global > default).

---

## Feature 22: RDW Vehicle Information API

### Purpose
Wrap the public RDW (Netherlands vehicle registration authority) Open Data API to look up vehicle details by license plate. Used by AI tool `lookup_vehicle_by_plate` and workshop endpoints.

### Endpoint (internal service)
- `RdwService.lookupByPlate(plate: string): Promise<VehicleInfo>` — fetches from `${RDW_API_BASE_URL}/m9d7-ebf2.json?kenteken={plate}`.
- Normalize plate (uppercase, strip dashes/spaces). Result includes: make, model, fuel type, first registration date, APK expiry, weight, color, vehicle category.
- Cache in Redis for 24h (`rdw:plate:{plate}`) and in DB on the Customer (`vehicleMake`, `vehicleModel`, `vehicleYear`).
- Handle 404 (unknown plate) gracefully — return `null`, don't throw.
- Send `X-App-Token: RDW_APP_TOKEN` header if configured.

### Public endpoint

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/vehicles/rdw/:plate` | Any | returns `{ plate, make, model, year, fuelType, apkExpiresAt, ... }` or 404 |

### Tests
- Caches result on second call.
- Unknown plate returns 404.
- Updates Customer.vehicleMake if Customer ID supplied via `?customerId=`.

---

## Feature 23: OAuth Connections (Google, Calendly)

### Purpose
Per-tenant OAuth tokens for Google Calendar and Calendly. Encrypted at rest.

### Data model
```prisma
model OAuthConnection {
  id            String   @id @default(cuid())
  tenantId      String
  tenant        Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  provider      OAuthProvider                                  // GOOGLE | CALENDLY
  externalAccountId String?                                    // e.g. Google calendar owner email
  accessToken   String                                         // encrypted
  refreshToken  String?                                        // encrypted
  expiresAt     DateTime?
  scope         String?
  metadata      Json     @default("{}")                         // e.g. { calendarId: "primary" }
  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt

  @@unique([tenantId, provider])
  @@index([tenantId])
}

enum OAuthProvider { GOOGLE CALENDLY }
```

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/integrations/oauth/google/authorize` | OWNER/ADMIN | returns `{ url }` (Google OAuth consent URL with state=encrypted tenantId) |
| GET | `/integrations/oauth/google/callback` | Public (state-verified) | exchanges code, stores tokens, redirects to dashboard |
| GET | `/integrations/oauth/calendly/authorize` | OWNER/ADMIN | similar |
| GET | `/integrations/oauth/calendly/callback` | Public (state-verified) | similar |
| GET | `/integrations/oauth` | OWNER/ADMIN | list current connections (no token bodies) |
| DELETE | `/integrations/oauth/:provider` | OWNER/ADMIN | disconnect |

### Token refresh
- Helper `OAuthService.getValidAccessToken(tenantId, provider)`: returns cached token if not within 5 min of expiry; else refreshes using `refreshToken` and persists.

### Tests
- Tokens encrypted in DB (raw SELECT shows ciphertext).
- State parameter prevents CSRF (random nonce + tenantId, signed).
- Refresh updates expiresAt.

---

## Feature 24: Google Calendar Integration

### Purpose
Push appointments into the dealer's Google Calendar and read availability for AI slot suggestions.

### Endpoints (internal service)
- `GoogleCalendarService.createEvent(tenantId, appointment)` — POSTs to `calendar/v3/calendars/{calendarId}/events`. Sets `summary`, `description`, `start.dateTime`, `end.dateTime`, `attendees: [{email: customer.email}]` (if present).
- `updateEvent(tenantId, googleEventId, patch)`.
- `deleteEvent(tenantId, googleEventId)`.
- `freeBusy(tenantId, fromIso, toIso, durationMinutes)` — calls `freeBusy.query`; returns available slots aligned to `slotIntervalMinutes` (default 30) within business hours.

### Configuration
- `metadata.calendarId` per OAuthConnection (default `"primary"`).
- `metadata.slotIntervalMinutes` (default 30).

### Public endpoint

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/integrations/google-calendar/availability` | MANAGER+ | `?type=APK&from=...&to=...` → array of slots |

### Tests
- Stub Google API; assert create/update/delete flows.
- freeBusy returns only slots in business hours.

---

## Feature 25: Calendly Integration

### Purpose
Optionally accept Calendly as an external scheduling source. Sync new Calendly bookings into Appointments.

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/integrations/calendly/webhooks` | Calendly signature | inbound webhook for `invitee.created` / `invitee.canceled` |
| GET | `/integrations/calendly/event-types` | MANAGER+ | list event types from the connected Calendly account |
| POST | `/integrations/calendly/configure` | OWNER/ADMIN | `{ eventTypeMappings: [{ calendlyEventTypeUri, appointmentType: AppointmentType }] }` (stored in `IntegrationConfig`) |

### Business logic
- On `invitee.created`: identify tenant from webhook signing key or by stored Calendly user URI in `OAuthConnection.externalAccountId`. Find/create Customer by email/phone from payload. Create Appointment with `source=CALENDLY`, `calendlyEventUri` set, type from mapping (default OTHER).
- On `invitee.canceled`: cancel the Appointment.

### Tests
- Signature verified; bad signature 403.
- Mapping picks correct AppointmentType.

---

## Feature 26: Autoflex DMS Integration (structure)

### Purpose
Pluggable DMS (Dealer Management System) integration scaffold. Initial implementation: read-only vehicle inventory from Autoflex; structured so other DMSes can be added.

### Data model
```prisma
model IntegrationConfig {
  id           String   @id @default(cuid())
  tenantId     String
  tenant       Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  provider     IntegrationProvider                           // AUTOFLEX | INVENTORY_SCRAPER | CALENDLY_MAPPING
  enabled      Boolean  @default(true)
  config       Json     @default("{}")                       // encrypted secrets if any
  credentials  Json?                                          // encrypted: { apiKey, baseUrl, ... }
  createdAt    DateTime @default(now())
  updatedAt    DateTime @updatedAt
  lastSyncAt   DateTime?
  lastSyncStatus String?
  lastSyncError  String?

  @@unique([tenantId, provider])
}

enum IntegrationProvider { AUTOFLEX INVENTORY_SCRAPER CALENDLY_MAPPING }

model VehicleStock {
  id            String   @id @default(cuid())
  tenantId      String
  tenant        Tenant   @relation(fields: [tenantId], references: [id], onDelete: Cascade)

  externalId    String                                       // ID at source (Autoflex / scraper)
  source        String                                       // "autoflex" | "scraper"

  make          String
  model         String
  year          Int?
  variant       String?
  priceCents    Int?
  currency      String   @default("EUR")
  mileageKm     Int?
  fuelType      String?
  transmission  String?
  bodyType      String?
  color         String?
  vin           String?
  plate         String?
  images        String[] @default([])
  url           String?
  status        String   @default("AVAILABLE")               // AVAILABLE | RESERVED | SOLD | WITHDRAWN

  rawData       Json?

  firstSeenAt   DateTime @default(now())
  lastSeenAt    DateTime @default(now())
  createdAt     DateTime @default(now())
  updatedAt     DateTime @updatedAt

  @@unique([tenantId, source, externalId])
  @@index([tenantId, status])
  @@index([tenantId, make, model])
}
```

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/integrations/configs` | OWNER/ADMIN | list all IntegrationConfig for tenant |
| PUT | `/integrations/configs/:provider` | OWNER/ADMIN | upsert |
| POST | `/integrations/autoflex/sync` | OWNER/ADMIN | trigger an on-demand sync (enqueues job) |
| GET | `/vehicles/stock` | Any | list current inventory (filter by make/model/price range/status) |
| GET | `/vehicles/stock/:id` | Any | detail |

### Business logic
- Strategy pattern: `IntegrationAdapter` interface with `sync(tenantId): Promise<SyncResult>`. Implementations: `AutoflexAdapter`, `InventoryScraperAdapter` (stub).
- Scheduled cron: every 6 hours, iterate enabled `IntegrationConfig` rows, enqueue `inventory-sync` job per tenant.
- Sync job: pulls latest, upserts by `(tenantId, source, externalId)`, marks missing items `WITHDRAWN`, updates `lastSyncAt/Status/Error`.

### Tests
- Adapter contract test.
- Sync job upserts and marks withdrawn correctly with stubbed adapter.


# PART E — SECURITY, COMPLIANCE, JOBS, OPS

---

## Feature 27: Audit Logs

### Purpose
Record every sensitive mutation: who, what, when, before/after for select fields.

### Data model
```prisma
model AuditLog {
  id          String   @id @default(cuid())
  tenantId    String?                                          // null for admin actions
  tenant      Tenant?  @relation(fields: [tenantId], references: [id])
  userId      String?
  user        User?    @relation(fields: [userId], references: [id])
  adminUserId String?
  adminUser   AdminUser? @relation(fields: [adminUserId], references: [id])

  action      String                                            // e.g. "lead.update", "user.invite", "appointment.cancel", "tenant.suspend"
  entityType  String                                            // "Lead", "User", ...
  entityId    String?
  before      Json?
  after       Json?
  ipAddress   String?
  userAgent   String?
  correlationId String?

  createdAt   DateTime @default(now())

  @@index([tenantId, createdAt(sort: Desc)])
  @@index([entityType, entityId])
  @@index([adminUserId, createdAt(sort: Desc)])
}
```

### Implementation
- `AuditService.record({ action, entityType, entityId, before, after })` called from service-layer mutations. Use an interceptor `@Audit('lead.update', 'Lead')` decorator that wraps the call: captures `before` via a fetch, runs, captures `after`, records.
- Redact sensitive fields (`passwordHash`, encrypted token fields) from `before`/`after`.
- Logged actions (non-exhaustive but required at minimum): user.create, user.update, user.delete, user.invite, customer.create, customer.update, customer.delete, customer.merge, lead.create, lead.update, lead.assign, lead.status, appointment.create, appointment.update, appointment.cancel, appointment.reschedule, settings.update, integration.connect, integration.disconnect, integration.config.update, whatsapp.config.update, subscription.update, tenant.suspend, tenant.reactivate, user.impersonate.

### API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/audit-logs` | OWNER/ADMIN | tenant's own audit logs, paginated, filter by action/entity/dateRange |
| GET | `/admin/audit-logs` | Admin | cross-tenant, all logs |

### Tests
- Every required action writes a log.
- Logs immutable (no UPDATE/DELETE endpoints).

---

## Feature 28: GDPR-Conscious Data Handling

### Purpose
Support GDPR rights: data export (right to access/portability), data deletion (right to erasure), consent tracking, data retention.

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/gdpr/customer-export/:customerId` | ADMIN+ | enqueues export job; produces a JSON file with all data for that customer; emails download link |
| POST | `/gdpr/customer-erasure/:customerId` | ADMIN+ | `{ reason }` — hard-deletes customer PII (keeps anonymized analytics rows) |
| POST | `/gdpr/tenant-export` | OWNER | full tenant data export (JSON + CSV bundle) |
| POST | `/gdpr/tenant-erasure` | OWNER | request tenant deletion; queued for 30-day grace period |

### Business logic
- **Customer export** job: gathers Customer, Leads, Conversations, Messages, Appointments, Notes → zips JSON files → uploads to S3 with a 7-day signed URL → emails the requester.
- **Customer erasure**:
  - Replace `firstName`, `lastName`, `email`, `phoneE164` (with `+0deleted{customerId}`), `licensePlate`, `vehicleVin` with NULL or anonymized markers.
  - Redact Message bodies that contain PII (replace `body` with `[redacted]`); keep `direction`, `timestamps`, `tokenCounts`.
  - Set `Customer.deletedAt` and add an `AuditLog` with action `customer.erasure`.
  - Cannot be reversed.
- **Tenant erasure**: schedule a job 30 days out; in the meantime tenant marked `CANCELED`; admin can cancel the request within the grace period. Job hard-deletes all tenant-scoped rows except `AuditLog` (retained 7 years for legal).
- **Consent**: tracked on Customer; if `consentMarketing=false`, no proactive/marketing AI outreach (the AI engine reads this).
- **Data retention defaults** (enforced by nightly job):
  - Messages older than 24 months purged (configurable per tenant).
  - WebhookEvent records older than 90 days purged.
  - RefreshToken: revoked & expired tokens older than 30 days deleted.
  - Notification: read notifications older than 90 days deleted.

### Tests
- Export job produces a valid zip and signed URL.
- Erasure replaces PII; subsequent fetch shows anonymized data.
- Retention job deletes only expected rows.

---

## Feature 29: Background Jobs (BullMQ)

### Purpose
All long-running, retryable, scheduled work runs in BullMQ queues, isolated from request lifecycle.

### Queues & jobs

| Queue | Job | Trigger | Notes |
|---|---|---|---|
| `whatsapp` | `media-persist` | inbound message with media | retries 3, backoff 30s |
| `ai` | `ai-respond` | inbound message in AI-mode conversation | concurrency 10; retries 1 (do not double-send) |
| `ai` | `ai-summarize` | every 10 messages in a conversation | retries 2 |
| `appointments` | `appointment-reminder-24h` | scheduled (delay) on appointment create | scheduled per appointment |
| `appointments` | `appointment-reminder-2h` | scheduled (delay) on appointment create | scheduled per appointment |
| `integrations` | `inventory-sync` | cron every 6h, or on demand | per tenant |
| `billing` | `trial-expiry-check` | cron daily 00:00 UTC | suspends tenants past trial without sub |
| `billing` | `past-due-suspend` | cron daily | suspends tenants with invoice past due > 7 days |
| `email` | `send-email` | various (welcome, invite, reset, reminders) | retries 5, backoff 60s |
| `gdpr` | `customer-export`, `tenant-export`, `customer-erasure`, `tenant-erasure` | API trigger | low concurrency 2 |
| `retention` | `data-retention-purge` | cron daily 03:00 UTC | per-table purges per Feature 28 |
| `webhooks` | `webhook-replay` | admin trigger | uses original handler logic |

### Implementation requirements
- One worker process can run multiple queues; for production deploy at least one dedicated worker process separate from API process.
- Job idempotency: every job handler is idempotent. Where applicable (e.g. `ai-respond`), use a Redis lock keyed by `conversationId` with TTL to prevent concurrent generation.
- Failed jobs after max retries → write to dead-letter table `JobFailure { id, queue, name, payload, error, failedAt }` and create a `Notification` of type `SYSTEM` for platform admins.
- Observability: BullMQ board UI mounted at `/admin/jobs` behind admin auth.

### Tests
- Each job processor tested with stubbed dependencies.
- Lock prevents double `ai-respond` for the same conversation.

---

## Feature 30: Email Service

### Purpose
Transactional emails: welcome, invite, password reset, billing receipts, appointment reminders (fallback if customer has no WhatsApp), GDPR export links.

### Implementation
- Provider: Postmark or Resend (configurable). Env var `EMAIL_PROVIDER`, `EMAIL_API_KEY`, `EMAIL_FROM`.
- All emails enqueued via `email/send-email` job.
- Templates stored as MJML or React Email components, rendered at send time. Localized (nl/en) based on recipient's preferred language.
- Templates required:
  - Welcome (after register)
  - User invite
  - Password reset
  - Email verification (if enabled later)
  - Billing payment failed
  - Trial ending in 3 days
  - GDPR export ready (download link)
- Test mode: in non-prod, do not actually send; capture to a `OutboxEmail` table for inspection.

### Tests
- Job enqueues; outbox row created in test mode.
- Template renders without missing variables.

---

## Feature 31: Security Hardening

### Purpose
Apply the security baseline across the stack.

### Requirements
- **Headers**: helmet defaults + strict CSP for `/api/docs` (no inline scripts), HSTS (max-age=31536000; includeSubDomains; preload).
- **CORS**: dashboard origin only; `credentials: true` only if cookies used (default no).
- **Body limits**: JSON 1MB; multipart 16MB (for media uploads only).
- **TLS**: terminate at load balancer; reject non-HTTPS at app level via `Strict-Transport-Security` and `req.secure` middleware (configurable for local dev).
- **Secrets**: never in repo. `.env.example` committed; real envs in vault.
- **Dependency security**: `npm audit --omit=dev` in CI; fail build on high/critical.
- **SQL injection**: Prisma parameterizes everything; never use `$queryRawUnsafe` with user input.
- **NoSQL injection**: N/A (no Mongo).
- **Prototype pollution**: `forbidNonWhitelisted: true` in validation pipe; never assign user input directly to objects.
- **Open redirects**: validate `successUrl` / `cancelUrl` / `returnUrl` parameters in billing endpoints against an allowlist (dashboard domain + subpaths).
- **Time-of-check/time-of-use**: tenant isolation enforced at query layer (Prisma middleware), not just at controller checks.
- **Mass assignment**: every endpoint uses explicit DTOs; never spread `req.body` into Prisma calls.
- **Brute force**: throttling on auth + lockout (Feature 3).
- **Webhook auth**: signature verification on every webhook (Twilio, Stripe, Calendly, Google).
- **Admin endpoints**: separate scope JWT, separate IP allowlist optional via env.
- **Audit**: every privileged action logged (Feature 27).
- **Penetration test checklist** to run before launch (each item must pass):
  - Cross-tenant data access blocked
  - JWT tampering rejected
  - Refresh token reuse detection works
  - Webhook signature bypass blocked
  - File upload type validation works
  - SSRF prevented (RDW/OAuth callbacks only fetch allowlisted hosts)
  - Open redirect prevented
  - Rate limit on auth endpoints works
  - SQL/NoSQL injection attempts fail
  - Authorization checks present on every endpoint

### Tests
- Automated security test suite covers each checklist item.

---

## Feature 32: Health, Readiness, Observability

### Purpose
Operate and monitor the service in production.

### Requirements
- `/health` (Feature 1): liveness; cheap.
- `/ready`: DB ping + Redis ping + (optional) outbound check to Twilio status page; 503 if any dependency unhealthy.
- `/metrics` (Prometheus format), gated behind `METRICS_TOKEN` query param or admin auth. Exposes:
  - HTTP request count/latency/status by route
  - DB connection pool stats
  - BullMQ queue depths, job durations, failures
  - Outbound WhatsApp send counts and failures
  - OpenAI call count, latency, token counts
- Sentry integration: capture exceptions in API + workers with tenantId/userId tags.
- OpenTelemetry traces optional; spans for HTTP, DB, OpenAI, Twilio, Stripe calls.
- Correlation ID propagated to outbound HTTP calls via header.

### Tests
- Metrics endpoint emits expected counters.
- Ready endpoint flips to 503 when Redis disconnected.

---

## Feature 33: Backups & Disaster Recovery

### Purpose
Documented + scripted backup of Postgres and media storage.

### Requirements
- **Postgres**:
  - Daily logical dump (`pg_dump -Fc`) to S3 with 30-day retention.
  - Continuous WAL archiving (point-in-time recovery) for production.
  - Weekly restore drill in staging (scripted; report success).
- **Redis**: AOF + RDB snapshots; daily snapshot to S3.
- **Media (S3)**: bucket versioning enabled; lifecycle policy moves to Glacier after 90 days.
- **Restore runbook** documented in `docs/runbooks/restore.md`.

### Implementation
- Provide a `scripts/backup.sh` for manual backup.
- Provide a `scripts/restore.sh` accepting a dump file.
- CI nightly job verifies last backup exists and is non-empty.

---

## Feature 34: API Documentation (OpenAPI / Swagger)

### Purpose
A complete, accurate OpenAPI 3.1 spec generated from controllers/DTOs and served at `/api/docs`.

### Requirements
- Every endpoint, request body, response, and error documented.
- Schemas reference DTOs (no anonymous inline schemas for shared models).
- Examples included for at least one success and one error response per endpoint.
- Auth schemes documented: `bearerAuth` (JWT), `twilioSignature`, `stripeSignature`, `googleOauth`, `calendlyOauth`.
- Spec validated in CI with `swagger-cli validate`.
- A static JSON dump (`openapi.json`) generated at build time and committed to a `docs/` artifact for the frontend team.

---

## Feature 35: CI/CD

### Purpose
GitHub Actions pipeline: lint, typecheck, unit, integration, build, push image, deploy.

### Pipeline
- **On PR**: install deps (cached), `prettier --check`, `eslint`, `tsc --noEmit`, `prisma format`, `prisma validate`, unit tests, integration tests against ephemeral Postgres + Redis (services), upload coverage. Required to pass.
- **On merge to `main`**: previous steps + `prisma migrate deploy` against staging DB, build Docker image, push to registry tagged with commit SHA + `staging`, deploy to staging environment, run smoke tests.
- **On tag `v*.*.*`**: deploy to production after manual approval. Run `prisma migrate deploy`, blue/green or rolling.

### Quality gates
- Coverage minimums: lines 80%, branches 75% (configurable).
- Build fails on `tsc` errors, lint errors, prettier diffs, failed tests, `npm audit` high/critical.

---

# PART F — IMPLEMENTATION ORDER & ACCEPTANCE

---

## Implementation Order (matches the critical path)

1. **Feature 1** — Bootstrap
2. **Feature 2** — Tenant + isolation
3. **Feature 3** — Auth (dealer)
4. **Feature 4** — RBAC
5. **Feature 5** — Users
6. **Feature 6** — DealerSettings
7. **Feature 7** — Customers
8. **Feature 9** — Conversations *(model first, endpoints can follow Feature 10)*
9. **Feature 10** — Messages
10. **Feature 11** — Notes
11. **Feature 12** — WhatsApp/Twilio (incl. inbound webhook)
12. **Feature 14** — Automation Rules (foundation needed before AI engine can suppress)
13. **Feature 13** — AI Engine
14. **Feature 15** — Notifications
15. **Feature 8** — Leads (after AI to wire auto-creation)
16. **Feature 16** — Appointments
17. **Feature 17** — Workshop Flows
18. **Feature 18** — Sales Flows
19. **Feature 22** — RDW
20. **Feature 23** — OAuth
21. **Feature 24** — Google Calendar
22. **Feature 25** — Calendly
23. **Feature 19** — Usage tracking
24. **Feature 20** — Stripe billing
25. **Feature 26** — DMS / inventory scaffold
26. **Feature 27** — Audit logs (retrofit decorators on already-built services)
27. **Feature 21** — Admin dashboard backend
28. **Feature 28** — GDPR
29. **Feature 29** — BullMQ jobs (most jobs wired in earlier features; this is the consolidation/test step)
30. **Feature 30** — Email service
31. **Feature 31** — Security hardening pass
32. **Feature 32** — Observability
33. **Feature 33** — Backups/DR
34. **Feature 34** — OpenAPI docs final pass
35. **Feature 35** — CI/CD

## Definition of Done (per feature)

A feature is "done" only when:
- All listed endpoints exist with correct auth + RBAC.
- Prisma migration applied; schema in repo matches DB.
- DTOs cover every input/output with class-validator decorators.
- All listed business rules implemented.
- All listed tests pass; coverage thresholds met.
- Swagger doc reflects the endpoints with examples.
- Audit logging (where required) firing.
- Logged in the project README under "Implemented features".

## Definition of Done (MVP launch)

- All MVP features (Features 1–20, 22–24, 27–35) green.
- E2E test scenario passes end-to-end on staging:
  1. New dealer registers (trial starts).
  2. Owner configures business hours, persona, connects Google Calendar, sets WhatsApp number (shared creds).
  3. Customer sends a WhatsApp "Hi, I need an APK for plate 12-ABC-3 next week".
  4. AI replies in Dutch, classifies intent, looks up plate via RDW, proposes 3 Google Calendar slots.
  5. Customer picks a slot; AI books it; Google event created; WhatsApp confirmation sent.
  6. Dealer sees the new Lead + Appointment in dashboard data (API responses verified).
  7. Reminder T-24h fires (time-travel test).
  8. Owner upgrades to Pro plan via Checkout; subscription webhook updates state.
  9. Usage record reflects messages + tokens.
- Pen-test checklist (Feature 31) passes.
- Disaster recovery drill (Feature 33) restores a backup successfully in staging.
- Admin can list, suspend, and impersonate the tenant.
- All audit logs present for the above flow.

---

# APPENDIX A — Full Prisma Schema (consolidated)

The features above introduce the schema incrementally. The final consolidated `schema.prisma` must include every model defined in this document with all relations bidirectionally declared. Implementers MUST consolidate and run `prisma format` and `prisma validate` before the first migration.

# APPENDIX B — Standard Error Codes

| Code | Meaning |
|---|---|
| `ValidationError` | 400 — input failed validation |
| `UnauthorizedError` | 401 — missing/invalid auth |
| `ForbiddenError` | 403 — authenticated but not permitted |
| `NotFoundError` | 404 — entity not found (or cross-tenant access) |
| `ConflictError` | 409 — state conflict (duplicate, illegal transition, last owner) |
| `RateLimitError` | 429 — throttled |
| `TenantSuspendedError` | 403 — tenant suspended |
| `UsageLimitError` | 402 — plan limit reached |
| `IntegrationError` | 502 — upstream provider error |
| `InternalError` | 500 — unexpected |

# APPENDIX C — Common DTO Conventions

- Date/time over the wire: ISO-8601 UTC strings; backend converts to `DateTime`.
- IDs: cuid strings.
- Money: integer cents + ISO currency code.
- Phones: E.164 strings.
- Enums: SCREAMING_SNAKE_CASE strings.
- Pagination envelope (§3.7) used uniformly.
- Partial-update endpoints use `PATCH`; full replacement uses `PUT`.

# APPENDIX D — Out of Scope for This PRD

The following are intentionally out of scope and covered elsewhere:
- Frontend dashboard implementation (Next.js).
- n8n workflow definitions (the backend exposes endpoints n8n consumes; the workflows themselves are a separate PRD).
- Mobile apps.
- Deep analytics beyond `/admin/analytics/overview` and `/usage/*`.
- Package/bundle product (post-MVP).
- Advanced DMS write-back (post-MVP; current scope is read-only inventory).