# restaurante_api/scaneats/pagination.py
from rest_framework.pagination import PageNumberPagination


class PaginacionEstandar(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200
