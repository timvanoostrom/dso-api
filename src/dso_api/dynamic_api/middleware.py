class BaseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


class DatasetMiddleware(BaseMiddleware):
    """
    Assign `dataset` to request, for easy access.
    """

    def process_view(self, request, view_func, view_args, view_kwargs):
        """
        Make current dataset available across whole application.
        """
        if not hasattr(request, "dataset"):
            try:
                request.dataset = view_func.cls.model._dataset_schema
            except AttributeError:
                pass

        return None


class TemporalDatasetMiddleware(BaseMiddleware):
    """
    Assign `versioned`, `dateset_verison` and `temporal_slice` to request.
    """

    def process_view(self, request, view_func, view_args, view_kwargs):

        request.versioned = False
        request.dataset_version = None
        request.dataset_temporal_slice = None

        if not hasattr(request, "dataset") or request.dataset.temporal is None:
            return None

        request.versioned = True
        if request.GET.get(request.dataset.temporal["identifier"]):
            request.dataset_version = request.GET.get(
                request.dataset.temporal["identifier"]
            )

        if "dimensions" in request.dataset.temporal:
            for key, fields in request.dataset.temporal["dimensions"].items():
                if request.GET.get(key):
                    request.dataset_temporal_slice = dict(
                        key=key, value=request.GET.get(key), fields=fields
                    )

        return None
