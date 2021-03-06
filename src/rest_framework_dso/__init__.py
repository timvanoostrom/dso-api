"""Reference implementation for DSO-compliant API's in Python with Django-Rest-Framework.

DSO = Digitaal Stelsel Omgevingswet.
This is a Dutch standard for API's for the government in The Netherlands:
https://aandeslagmetdeomgevingswet.nl/digitaal-stelsel/aansluiten/standaarden/api-en-uri-strategie/

Which is updated as "NL Designrules":
https://docs.geostandaarden.nl/api/API-Strategie-ext/

Implemented:

* HAL links {"_links": {"self": {"href": ..., "title": ...}}}
* HAL ``?_expandScope=field1,field2`` -> gives ``_embedded`` field in response.
* The ``?_expand=true`` option to expand all fields
# The ``?_fields=...`` option to define which fields to return.
* No envelope for single-object / detail views.

Via other packages:

* Enforce fields in camelCase -> use djangorestframework-camel-case

Not implemented:

* ?_fields=field1.subfield
* ?_find=urgent (search queries, including ``*`` and ``?`` wildcards for single words)
* GeoJSON support.

Extra recommendations:

* Use base64-encoded UUID's (=22 characters).

Mandatory settings:

REST_FRAMEWORK = dict(
    DEFAULT_PAGINATION_CLASS="rest_framework_dso.pagination.DSOPageNumberPagination",
    DEFAULT_PARSER_CLASSES=[
        "rest_framework_dso.parsers.HALJSONParser",
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    DEFAULT_RENDERER_CLASSES=[
        "rest_framework_dso.renderers.HALJSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",  # <-- optional
    ],
)
"""
