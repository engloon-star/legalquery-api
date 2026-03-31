"""
LegalQuery MY — FastAPI Backend
=================================
Handles: search, download, credits, Stripe webhooks, auth

SETUP:
    pip install fastapi uvicorn httpx stripe python-dotenv

DEPLOY: Railway.app
    railway init && railway up
    Set all environment variables in Railway dashboard

RUN LOCALLY:
    uvicorn main:app --reload --port 8000
"""

import os
import hmac
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, List

import httpx
import stripe
from fastapi import FastAPI, HTTPException, Header, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_ANON_KEY   = os.getenv("SUPABASE_ANON_KEY")
ES_URL              = os.getenv("ELASTICSEARCH_URL")
ES_API_KEY          = os.getenv("ELASTICSEARCH_API_KEY")
ES_INDEX            = "malaysian_judgments"
STRIPE_SECRET_KEY   = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
AWS_BUCKET          = os.getenv("AWS_S3_BUCKET", "legalquery-judgments")
AWS_REGION          = os.getenv("AWS_REGION", "ap-southeast-1")
AWS_ACCESS_KEY      = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY      = os.getenv("AWS_SECRET_ACCESS_KEY")
FRONTEND_URL        = os.getenv("FRONTEND_URL", "https://legalquery.my")

stripe.api_key = STRIPE_SECRET_KEY

# Credit costs per action
CREDIT_COSTS = {
    "search":      1,
    "download":    5,
    "export_csv":  3,
    "ai_summary":  2,
}

app = FastAPI(
    title="LegalQuery MY API",
    description="Malaysian court judgment search platform",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────
# SUPABASE CLIENT HELPERS
# ─────────────────────────────────────────
async def supabase_get(path: str, params: dict = None, token: str = None) -> dict:
    """GET request to Supabase REST API."""
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {token or SUPABASE_SERVICE_KEY}",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=headers,
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


async def supabase_post(path: str, data: dict, token: str = None) -> dict:
    """POST request to Supabase REST API."""
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {token or SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=headers,
            json=data,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


async def supabase_rpc(func: str, params: dict) -> dict:
    """Call a Supabase RPC (stored function)."""
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{func}",
            headers=headers,
            json=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────
# AUTH MIDDLEWARE
# ─────────────────────────────────────────
async def get_current_user(authorization: str = Header(...)) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")

    token = authorization.replace("Bearer ", "").strip()

    try:
        # Step 1 — verify token with Supabase
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {token}",
                },
                timeout=15,
            )
        
        log.info("Supabase auth response: %d", resp.status_code)
        
        if resp.status_code != 200:
            log.error("Auth failed: %s", resp.text)
            raise HTTPException(401, f"Token rejected by Supabase: {resp.text}")

        user_data = resp.json()
        user_id = user_data.get("id")
        
        if not user_id:
            raise HTTPException(401, "No user ID in token")

        log.info("User authenticated: %s", user_id)

        # Step 2 — get profile from database
        async with httpx.AsyncClient() as client:
            profile_resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/profiles",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                },
                params={"id": f"eq.{user_id}", "select": "*"},
                timeout=10,
            )

        log.info("Profile response: %d %s", profile_resp.status_code, profile_resp.text[:200])
        profiles = profile_resp.json()

        # Step 3 — create profile if missing
        if not profiles:
            log.info("Profile not found — creating for user %s", user_id)
            async with httpx.AsyncClient() as client:
                create_resp = await client.post(
                    f"{SUPABASE_URL}/rest/v1/profiles",
                    headers={
                        "apikey": SUPABASE_SERVICE_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "return=representation",
                    },
                    json={
                        "id": user_id,
                        "email": user_data.get("email", ""),
                        "credit_balance": 10,
                        "subscription_tier": "free",
                    },
                    timeout=10,
                )
            log.info("Profile created: %d %s", create_resp.status_code, create_resp.text[:200])
            profiles = create_resp.json()

        if not profiles:
            raise HTTPException(500, "Could not find or create profile")

        return profiles[0] if isinstance(profiles, list) else profiles

    except HTTPException:
        raise
    except Exception as e:
        log.error("Auth error: %s", e)
        raise HTTPException(401, f"Authentication failed: {str(e)}")


# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500,
                       description="Search query with AND/OR/NOT operators")
    court_level: Optional[str] = None
    year_from:   Optional[int] = None
    year_to:     Optional[int] = None
    state:       Optional[str] = None
    outcome:     Optional[str] = None
    subject_tag: Optional[str] = None
    source:      Optional[str] = None
    page:        int = Field(default=1, ge=1)
    page_size:   int = Field(default=20, ge=1, le=50)


class SearchResult(BaseModel):
    case_id:       str
    citation:      Optional[str]
    case_name:     Optional[str]
    court_level:   Optional[str]
    state:         Optional[str]
    decision_date: Optional[str]
    judges:        List[str]
    outcome:       Optional[str]
    subject_tags:  List[str]
    source:        str
    snippet:       Optional[str]      # highlighted excerpt
    score:         float


class SearchResponse(BaseModel):
    results:         List[SearchResult]
    total:           int
    page:            int
    pages:           int
    credits_used:    int
    credits_remaining: int
    query_time_ms:   int


# ─────────────────────────────────────────
# ELASTICSEARCH QUERY BUILDER
# ─────────────────────────────────────────
def build_es_query(req: SearchRequest) -> dict:
    """
    Convert user's query string (with AND/OR/NOT) into
    Elasticsearch bool query. Supports wildcards.

    Examples:
        "negligence AND duty of care"
        "section 13 OR section 14" AND "Employment Act"
        "defamation" NOT "acquitted"
        "Tan Sri*" AND fraud
    """
    # Use Elasticsearch query_string which natively supports
    # AND, OR, NOT, wildcards (*,?), phrase ("exact phrase"), grouping
    must_clauses = [
        {
            "query_string": {
                "query": req.query,
                "fields": [
                    "full_text^1",       # boost: full text
                    "case_name^3",       # boost: case name matches rank higher
                    "citation^5",        # boost: citation exact matches highest
                    "judges^2",
                    "subject_tags^2",
                ],
                "default_operator": "AND",
                "allow_leading_wildcard": False,
                "analyze_wildcard": True,
                "fuzziness": "AUTO",
            }
        }
    ]

    # Filters (don't affect relevance score — fast)
    filter_clauses = []

    if req.court_level:
        filter_clauses.append({"term": {"court_level": req.court_level}})

    if req.year_from or req.year_to:
        year_range = {}
        if req.year_from:
            year_range["gte"] = req.year_from
        if req.year_to:
            year_range["lte"] = req.year_to
        filter_clauses.append({"range": {"decision_year": year_range}})

    if req.state:
        filter_clauses.append({"term": {"state.keyword": req.state}})

    if req.outcome:
        filter_clauses.append({"term": {"outcome": req.outcome}})

    if req.subject_tag:
        filter_clauses.append({"term": {"subject_tags": req.subject_tag}})

    if req.source:
        filter_clauses.append({"term": {"source": req.source}})

    return {
        "query": {
            "bool": {
                "must":   must_clauses,
                "filter": filter_clauses,
            }
        },
        "highlight": {
            "fields": {
                "full_text": {
                    "fragment_size": 250,
                    "number_of_fragments": 2,
                    "pre_tags":  ["<mark>"],
                    "post_tags": ["</mark>"],
                }
            }
        },
        "from": (req.page - 1) * req.page_size,
        "size": req.page_size,
        "_source": [
            "case_id", "citation", "case_name", "court_level",
            "state", "decision_date", "judges", "outcome",
            "subject_tags", "source",
        ],
    }


# ─────────────────────────────────────────
# ROUTES: SEARCH
# ─────────────────────────────────────────
@app.post("/api/search", response_model=SearchResponse)
async def search_cases(
    req: SearchRequest,
    user: dict = Depends(get_current_user),
    request: Request = None,
):
    """
    Full-text boolean search across all judgments.
    Costs 1 credit per query.
    """
    import time
    start_ms = int(time.time() * 1000)

    # Deduct credit BEFORE search (atomic)
    deduction = await supabase_rpc("deduct_credits", {
        "p_user_id":       user["id"],
        "p_credits":       CREDIT_COSTS["search"],
        "p_action_type":   "search",
        "p_query_text":    req.query,
    })

    if not deduction.get("success"):
        raise HTTPException(
            402,
            detail={
                "error": deduction.get("error", "Insufficient credits"),
                "balance": deduction.get("balance", 0),
                "required": CREDIT_COSTS["search"],
            }
        )

    # Execute Elasticsearch query
    es_query = build_es_query(req)

    try:
        async with httpx.AsyncClient() as client:
            es_resp = await client.post(
                f"{ES_URL}/{ES_INDEX}/_search",
                headers={
                    "Authorization": f"ApiKey {ES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=es_query,
                timeout=15,
            )
            es_resp.raise_for_status()
            es_data = es_resp.json()

    except Exception as e:
        log.error("Elasticsearch error: %s", e)
        # Refund credit on search engine failure
        await supabase_rpc("add_credits", {
            "p_user_id": user["id"],
            "p_credits": CREDIT_COSTS["search"],
            "p_transaction_type": "refund",
            "p_description": "Search engine error refund",
        })
        raise HTTPException(503, "Search engine temporarily unavailable")

    hits = es_data.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    results = []

    for hit in hits.get("hits", []):
        src = hit["_source"]
        highlight = hit.get("highlight", {})
        snippet_parts = highlight.get("full_text", [])
        snippet = " … ".join(snippet_parts) if snippet_parts else None

        results.append(SearchResult(
            case_id      = src.get("case_id", hit["_id"]),
            citation     = src.get("citation"),
            case_name    = src.get("case_name"),
            court_level  = src.get("court_level"),
            state        = src.get("state"),
            decision_date= src.get("decision_date"),
            judges       = src.get("judges", []),
            outcome      = src.get("outcome"),
            subject_tags = src.get("subject_tags", []),
            source       = src.get("source", ""),
            snippet      = snippet,
            score        = hit.get("_score", 0.0),
        ))

    elapsed_ms = int(time.time() * 1000) - start_ms

    # Update result count in usage log
    # (fire-and-forget — don't await)

    return SearchResponse(
        results           = results,
        total             = total,
        page              = req.page,
        pages             = max(1, (total + req.page_size - 1) // req.page_size),
        credits_used      = CREDIT_COSTS["search"],
        credits_remaining = deduction["balance"],
        query_time_ms     = elapsed_ms,
    )


# ─────────────────────────────────────────
# ROUTES: DOWNLOAD
# ─────────────────────────────────────────
@app.post("/api/download/{case_id}")
async def initiate_download(
    case_id: str,
    user: dict = Depends(get_current_user),
):
    """
    Deduct 5 credits and return a temporary signed S3 URL.
    URL expires in 5 minutes.
    """
    # Check case exists
    cases = await supabase_get(
        "cases",
        params={"id": f"eq.{case_id}", "select": "id,pdf_s3_key,citation,case_name"},
    )
    if not cases:
        raise HTTPException(404, "Case not found")
    case = cases[0]

    if not case.get("pdf_s3_key"):
        raise HTTPException(404, "PDF not available for this case")

    # Deduct credits
    deduction = await supabase_rpc("deduct_credits", {
        "p_user_id":     user["id"],
        "p_credits":     CREDIT_COSTS["download"],
        "p_action_type": "download",
        "p_case_id":     case_id,
    })

    if not deduction.get("success"):
        raise HTTPException(
            402,
            detail={
                "error": deduction.get("error", "Insufficient credits"),
                "balance": deduction.get("balance", 0),
                "required": CREDIT_COSTS["download"],
            }
        )

    # Generate presigned S3 URL (expires in 5 min)
    try:
        import boto3
        s3 = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY,
        )
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": AWS_BUCKET,
                "Key": case["pdf_s3_key"],
                "ResponseContentDisposition": (
                    f'attachment; filename="{case.get("citation", case_id)}.pdf"'
                ),
            },
            ExpiresIn=300,  # 5 minutes
        )
    except Exception as e:
        log.error("S3 presign failed: %s", e)
        # Refund on failure
        await supabase_rpc("add_credits", {
            "p_user_id": user["id"],
            "p_credits": CREDIT_COSTS["download"],
            "p_transaction_type": "refund",
            "p_description": "Download failure refund",
        })
        raise HTTPException(503, "Download service temporarily unavailable")

    return {
        "download_url":     presigned_url,
        "expires_in":       300,
        "credits_used":     CREDIT_COSTS["download"],
        "credits_remaining": deduction["balance"],
        "case_name":        case.get("case_name"),
        "citation":         case.get("citation"),
    }


# ─────────────────────────────────────────
# ROUTES: CREDITS & BILLING
# ─────────────────────────────────────────
@app.get("/api/credits/balance")
async def get_credit_balance(user: dict = Depends(get_current_user)):
    """Return current credit balance and usage stats."""
    # Recent usage
    logs = await supabase_get(
        "usage_logs",
        params={
            "user_id":   f"eq.{user['id']}",
            "select":    "action_type,credits_deducted,created_at",
            "order":     "created_at.desc",
            "limit":     "10",
        },
    )
    return {
        "balance":           user["credit_balance"],
        "subscription_tier": user["subscription_tier"],
        "subscription_expires_at": user.get("subscription_expires_at"),
        "total_searches":    user.get("total_searches", 0),
        "total_downloads":   user.get("total_downloads", 0),
        "recent_activity":   logs,
    }


@app.get("/api/credits/bundles")
async def get_credit_bundles():
    """Return available credit top-up packages."""
    bundles = await supabase_get(
        "credit_bundles",
        params={"is_active": "eq.true", "order": "sort_order.asc"},
    )
    return {"bundles": bundles}


@app.post("/api/credits/checkout")
async def create_checkout_session(
    bundle_id: str,
    user: dict = Depends(get_current_user),
):
    """
    Create a Stripe Checkout session for a credit bundle top-up.
    Returns checkout URL to redirect user to.
    """
    bundles = await supabase_get(
        "credit_bundles",
        params={"id": f"eq.{bundle_id}", "is_active": "eq.true"},
    )
    if not bundles:
        raise HTTPException(404, "Bundle not found")
    bundle = bundles[0]

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card", "fpx"],  # fpx = Malaysian online banking
            line_items=[{
                "price_data": {
                    "currency": "myr",
                    "product_data": {
                        "name": f"LegalQuery {bundle['name']} Pack",
                        "description": f"{bundle['credits']} search credits",
                    },
                    "unit_amount": int(bundle["price_myr"] * 100),  # cents
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{FRONTEND_URL}/credits?success=true&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/credits?cancelled=true",
            customer_email=user["email"],
            metadata={
                "user_id":   user["id"],
                "bundle_id": bundle_id,
                "credits":   str(bundle["credits"]),
            },
        )
        return {"checkout_url": session.url, "session_id": session.id}

    except stripe.error.StripeError as e:
        log.error("Stripe checkout error: %s", e)
        raise HTTPException(500, "Payment service error")


@app.get("/api/subscriptions/plans")
async def get_subscription_plans():
    """Return all active subscription plans."""
    plans = await supabase_get(
        "subscription_plans",
        params={"is_active": "eq.true", "order": "price_myr.asc"},
    )
    return {"plans": plans}


@app.post("/api/subscriptions/checkout")
async def create_subscription_checkout(
    plan_id: str,
    user: dict = Depends(get_current_user),
):
    """Create Stripe Checkout for a subscription plan."""
    plans = await supabase_get(
        "subscription_plans",
        params={"id": f"eq.{plan_id}", "is_active": "eq.true"},
    )
    if not plans:
        raise HTTPException(404, "Plan not found")
    plan = plans[0]

    if not plan.get("stripe_price_id"):
        raise HTTPException(400, "This plan is not available for online checkout")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card", "fpx"],
            line_items=[{"price": plan["stripe_price_id"], "quantity": 1}],
            mode="subscription",
            success_url=f"{FRONTEND_URL}/dashboard?subscribed=true",
            cancel_url=f"{FRONTEND_URL}/pricing?cancelled=true",
            customer_email=user["email"],
            metadata={
                "user_id": user["id"],
                "plan_id": plan_id,
                "tier":    plan["tier"],
                "credits": str(plan["credits_per_month"]),
            },
        )
        return {"checkout_url": session.url}

    except stripe.error.StripeError as e:
        log.error("Stripe subscription error: %s", e)
        raise HTTPException(500, "Payment service error")


# ─────────────────────────────────────────
# ROUTES: STRIPE WEBHOOK
# ─────────────────────────────────────────
@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    """
    Handle Stripe payment events.
    Credit the user's account on successful payment.
    IMPORTANT: Add this URL in your Stripe Dashboard →
               Developers → Webhooks
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        user_id = meta.get("user_id")
        credits = int(meta.get("credits", 0))
        payment_id = session.get("id")

        if user_id and credits > 0:
            result = await supabase_rpc("add_credits", {
                "p_user_id":           user_id,
                "p_credits":           credits,
                "p_transaction_type":  "topup",
                "p_stripe_payment_id": payment_id,
                "p_description":       f"Credit top-up: {credits} credits",
            })
            log.info("Credits added: %d to user %s (result: %s)",
                     credits, user_id, result)

    elif event["type"] == "invoice.payment_succeeded":
        # Monthly subscription renewal — add credits
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if sub_id:
            try:
                sub = stripe.Subscription.retrieve(sub_id)
                meta = sub.get("metadata", {})
                user_id = meta.get("user_id")
                credits = int(meta.get("credits", 0))
                tier = meta.get("tier", "basic")

                if user_id and credits > 0:
                    await supabase_rpc("add_credits", {
                        "p_user_id":           user_id,
                        "p_credits":           credits,
                        "p_transaction_type":  "subscription",
                        "p_stripe_payment_id": sub_id,
                        "p_description":       f"Monthly {tier} subscription renewal",
                    })
                    # Update subscription expiry in profile
                    async with httpx.AsyncClient() as client:
                        await client.patch(
                            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}",
                            headers={
                                "apikey": SUPABASE_SERVICE_KEY,
                                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                                "Content-Type": "application/json",
                            },
                            json={
                                "subscription_tier": tier,
                                "subscription_expires_at": (
                                    datetime.utcnow() + timedelta(days=35)
                                ).isoformat(),
                            },
                        )
            except Exception as e:
                log.error("Subscription renewal error: %s", e)

    elif event["type"] == "customer.subscription.deleted":
        # Downgrade to free on cancellation
        sub = event["data"]["object"]
        meta = sub.get("metadata", {})
        user_id = meta.get("user_id")
        if user_id:
            async with httpx.AsyncClient() as client:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}",
                    headers={
                        "apikey": SUPABASE_SERVICE_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={"subscription_tier": "free"},
                )

    return {"status": "ok"}


# ─────────────────────────────────────────
# ROUTES: CASE DETAILS
# ─────────────────────────────────────────
@app.get("/api/cases/{case_id}")
async def get_case_details(
    case_id: str,
    user: dict = Depends(get_current_user),
):
    """Get case metadata (free — no credits). Download requires credits."""
    cases = await supabase_get(
        "cases",
        params={
            "id": f"eq.{case_id}",
            "select": (
                "id,citation,case_name,court_level,state,"
                "decision_date,judges,outcome,subject_tags,"
                "source,word_count,created_at"
                # Note: full_text and pdf_s3_key NOT exposed here
            ),
        },
    )
    if not cases:
        raise HTTPException(404, "Case not found")
    return cases[0]


# ─────────────────────────────────────────
# ROUTES: HEALTH + ELASTICSEARCH SETUP
# ─────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
    }


@app.post("/api/admin/setup-elasticsearch")
async def setup_elasticsearch_index(
    x_admin_key: str = Header(...),
):
    """
    One-time setup: create Elasticsearch index with correct mappings.
    Call this once after deploying. Protect with admin key.
    """
    if x_admin_key != os.getenv("ADMIN_API_KEY"):
        raise HTTPException(403, "Forbidden")

    mapping = {
        "mappings": {
            "properties": {
                "case_id":      {"type": "keyword"},
                "full_text":    {
                    "type": "text",
                    "analyzer": "english",
                    "fields": {
                        "keyword": {"type": "keyword", "ignore_above": 256}
                    }
                },
                "case_name":    {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "citation":     {"type": "keyword"},
                "court_level":  {"type": "keyword"},
                "state":        {"type": "keyword"},
                "decision_year":{"type": "integer"},
                "decision_date":{"type": "date"},
                "judges":       {"type": "keyword"},
                "outcome":      {"type": "keyword"},
                "subject_tags": {"type": "keyword"},
                "source":       {"type": "keyword"},
                "word_count":   {"type": "integer"},
                "indexed_at":   {"type": "date"},
            }
        },
        "settings": {
            "number_of_shards":   1,
            "number_of_replicas": 1,
            "analysis": {
                "analyzer": {
                    "english": {
                        "tokenizer": "standard",
                        "filter": ["lowercase", "english_stemmer", "english_stop"],
                    }
                },
                "filter": {
                    "english_stemmer": {"type": "stemmer", "language": "english"},
                    "english_stop":    {"type": "stop",    "stopwords": "_english_"},
                }
            }
        }
    }

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{ES_URL}/{ES_INDEX}",
            headers={
                "Authorization": f"ApiKey {ES_API_KEY}",
                "Content-Type": "application/json",
            },
            json=mapping,
            timeout=15,
        )
        return {"status": resp.status_code, "body": resp.json()}
