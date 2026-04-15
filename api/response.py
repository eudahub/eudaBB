"""Helpers for the standard API response envelope.

All API responses follow the format documented in docs/api.md:
    {
        "status": "ok" | "error",
        "data": { ... } | [ ... ],
        "pagination": { ... },          # optional, only on paginated lists
        "error_code": "SOME_CODE",      # only on errors
        "error_message": "Human text"   # only on errors
    }
"""

from django.core.paginator import Paginator, EmptyPage
from rest_framework.response import Response
from rest_framework import status as http_status


def ok(data, pagination=None, http_code=http_status.HTTP_200_OK):
    body = {"status": "ok", "data": data}
    if pagination is not None:
        body["pagination"] = pagination
    return Response(body, status=http_code)


def created(data):
    return ok(data, http_code=http_status.HTTP_201_CREATED)


def error(error_code: str, error_message: str, http_code=http_status.HTTP_400_BAD_REQUEST):
    return Response(
        {
            "status": "error",
            "error_code": error_code,
            "error_message": error_message,
        },
        status=http_code,
    )


def paginate(queryset, request, serializer_class, per_page=20, **serializer_kwargs):
    """Paginate a queryset and return an ok() response with pagination metadata.

    Query params: ?page=1&per_page=20
    """
    try:
        per_page = int(request.query_params.get("per_page", per_page))
        per_page = max(1, min(per_page, 100))
    except (ValueError, TypeError):
        per_page = 20

    try:
        page_num = int(request.query_params.get("page", 1))
        page_num = max(1, page_num)
    except (ValueError, TypeError):
        page_num = 1

    paginator = Paginator(queryset, per_page)
    try:
        page = paginator.page(page_num)
    except EmptyPage:
        page = paginator.page(paginator.num_pages)

    data = serializer_class(page.object_list, many=True, **serializer_kwargs).data
    pagination = {
        "page": page.number,
        "per_page": per_page,
        "total_pages": paginator.num_pages,
        "total_items": paginator.count,
    }
    return ok(data, pagination=pagination)
