from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from .tracing import new_trace_id, set_trace_id


class TraceIdMiddleware:
    """Assign a trace_id to every request so it can be correlated across
    performance_metrics, application logs, and db_span logs. Must run early so
    downstream middleware/log calls already see the value via contextvars."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)

        trace_id = new_trace_id()
        set_trace_id(trace_id)
        response = self.get_response(request)
        response["X-Trace-Id"] = trace_id
        return response

    async def __acall__(self, request):
        trace_id = new_trace_id()
        set_trace_id(trace_id)
        response = await self.get_response(request)
        response["X-Trace-Id"] = trace_id
        return response
