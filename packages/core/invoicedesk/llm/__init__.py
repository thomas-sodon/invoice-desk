"""LLM clients. ``anthropic_client`` (real SDK, owned by the `extraction` component) is the only
module allowed to import ``anthropic``. Fakes live in ``invoicedesk.fakes.llm``."""
