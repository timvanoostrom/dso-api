"""API filtering.

This implements the filtering and ordering spec.
DSO 1.1 Spec: "2.6.6 Filteren, sorteren en zoeken"
"""
import re
import operator
from datetime import datetime
from functools import reduce
from typing import Type

from django import forms
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.gis.db import models as gis_models
from django.contrib.gis.geos import GEOSGeometry
from django.db import models
from django.db.models import Q, expressions, lookups
from django.forms import widgets
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django_filters import fields
from django_filters.constants import EMPTY_VALUES
from django_filters.rest_framework import DjangoFilterBackend, FilterSet, filters
from django_postgres_unlimited_varchar import UnlimitedCharField
from django.contrib.postgres.fields.array import ArrayField
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework_gis.filters import GeometryFilter
from schematools.utils import to_snake_case

__all__ = [
    "DSOFilterSet",
    "DSOFilterSetBackend",
    "DSOOrderingFilter",
    "RangeFilter",
]


def _valid_rd(x, y):
    """
    Check valid RD x, y coordinates
    """

    rd_x_min = 0
    rd_y_min = 300000
    rd_x_max = 280000
    rd_y_max = 625000

    if not rd_x_min <= x <= rd_x_max:
        return False

    if not rd_y_min <= y <= rd_y_max:
        return False

    return True


def _valid_lat_lon(lat, lon):
    """
    Check if lat/lon is in the Netherlands bounding box
    """
    lat_min = 50.803721015
    lat_max = 53.5104033474
    lon_min = 3.31497114423
    lon_max = 7.09205325687

    if not lat_min <= lat <= lat_max:
        return False

    if not lon_min <= lon <= lon_max:
        return False

    return True


def _validate_convert_x_y(x, y, srid):
    fx = float(x)
    fy = float(y)
    x_lon = y_lat = None
    if not srid or srid == 4326:
        if _valid_lat_lon(fx, fy):
            x_lon = y
            y_lat = x
            srid = 4326
        elif _valid_lat_lon(fy, fx):
            x_lon = x
            y_lat = y
            srid = 4326
    if not srid or srid == 28992:
        if _valid_rd(fx, fy):
            x_lon = x
            y_lat = y
            srid = 28992
    elif srid not in (28992, 4326):
        x_lon = x
        y_lat = y
    return x_lon, y_lat, srid


@models.CharField.register_lookup
@models.TextField.register_lookup
class IsEmpty(lookups.Lookup):
    lookup_name = "isempty"

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = []
        not_negation = (
            "NOT " if len(rhs_params) == 1 and rhs_params[0] == "True" else ""
        )
        return f"({lhs} = '') IS {not_negation}FALSE", params


@models.Field.register_lookup
@models.ForeignObject.register_lookup
class NotEqual(lookups.Lookup):
    """Allow fieldname__not=... lookups in querysets."""

    lookup_name = "not"
    can_use_none_as_rhs = True

    def as_sql(self, compiler, connection):
        """Generate the required SQL."""
        # Need to extract metadata from lhs, so parsing happens inline
        lhs = self.lhs  # typically a Col(alias, target) object
        if hasattr(lhs, "resolve_expression"):
            lhs = lhs.resolve_expression(compiler.query)

        lhs_field = lhs
        while isinstance(lhs_field, expressions.Func):
            # Allow date_field__day__not=12 to return None values
            lhs_field = lhs_field.source_expressions[0]
        lhs_nullable = lhs_field.target.null

        # Generate the SQL-prepared values
        lhs, lhs_params = self.process_lhs(compiler, connection, lhs=lhs)  # (field, [])
        rhs, rhs_params = self.process_rhs(compiler, connection)  # ("%s", [value])

        if lhs_nullable and rhs is not None:
            # Allow field__not=value to return NULL fields too.
            return (
                f"({lhs} IS NULL OR {lhs} != {rhs})",
                lhs_params + lhs_params + rhs_params,
            )
        elif rhs_params and rhs_params[0] is None:
            # Allow field__not=None to work.
            return f"{lhs} IS NOT NULL", lhs_params
        else:
            return f"{lhs} != {rhs}", lhs_params + rhs_params


@models.CharField.register_lookup
@models.TextField.register_lookup
class Wildcard(lookups.Lookup):
    """Allow fieldname__wildcard=... lookups in querysets."""

    lookup_name = "wildcard"

    def as_sql(self, compiler, connection):
        """Generate the required SQL."""
        # lhs = "table"."field"
        # rhs = %s
        # lhs_params = []
        # lhs_params = ["prep-value"]
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} LIKE {rhs}", lhs_params + rhs_params

    def get_db_prep_lookup(self, value, connection):
        """Apply the wildcard logic to the right-hand-side value"""
        value = (
            value
            # Escape % and _ first.
            # Not using r"\" here as that is a syntax error.
            .replace("\\", "\\\\")
            .replace("%", r"\%")
            .replace("_", r"\_")
            # Replace wildcard chars with SQL LIKE logic
            .replace("*", "%")
            .replace("?", "_")
        )
        return "%s", [value]


class WildcardCharFilter(filters.CharFilter):
    """Char filter that uses the 'wildcard' lookup by default."""

    def __init__(self, field_name=None, lookup_expr="exact", **kwargs):
        # Make sure that only the "__exact" lookup is translated into a
        # wildcard lookup. Passing 'lookup_expr' in FILTER_DEFAULTS's "extra"
        # field overrides all chosen lookup types.
        if lookup_expr == "exact":
            lookup_expr = "wildcard"
        super().__init__(field_name, lookup_expr, **kwargs)


class MultipleValueWidget(widgets.Input):
    """Widget to retrieve all GET parameters instead of just a single value."""

    def value_from_datadict(self, data, files, name):
        try:
            return data.getlist(name)
        except AttributeError:
            # Unit testing with dict input
            value = data.get(name)
            return [value] if value is not None else None


class MultipleValueField(forms.Field):
    """Form field that returns all values."""

    default_error_messages = {"required": "Please specify one or more values."}
    widget = MultipleValueWidget

    def __init__(self, subfield: forms.Field, **kwargs):
        safe_kwargs = {
            k: kwargs.get(k, getattr(subfield, k, None))
            for k in (
                "required",
                "widget",
                "label",
                "initial",
                "help_text",
                "error_messages",
                "show_hidden_initial",
                "validators",
                "localize",
                "disabled",
                "label_suffix",
            )
        }
        super().__init__(**safe_kwargs)
        self.subfield = subfield

        # Enforce the "getlist" retrieval, even when a different widget was used.
        # The "__get__" is needed to retrieve the MethodType instead of the unbound function.
        if not isinstance(self.widget, MultipleValueWidget):
            self.widget.value_from_datadict = MultipleValueWidget.value_from_datadict.__get__(
                self.widget
            )

    def clean(self, values):
        if not values:
            if self.required:
                raise DjangoValidationError(self.error_messages["required"])
            else:
                return []

        if not isinstance(values, list):
            raise RuntimeError(
                "MultipleValueField.widget does not return list of values"
            )

        result = []
        errors = []
        for i, value in enumerate(values):
            try:
                result.append(self.subfield.clean(value))
            except DjangoValidationError as e:
                errors.append(e)

        if errors:
            raise DjangoValidationError(errors)

        return result


class MultipleValueFilter(filters.Filter):
    """Allow a value to be included multiple times"""

    field_class = MultipleValueField
    OPERATORS = {
        "AND": operator.and_,
        "OR": operator.or_,
    }

    def __init__(self, value_filter: filters.Filter, operator="AND"):
        super().__init__(
            # Copy settings from wrapped field
            field_name=value_filter.field_name,
            lookup_expr=value_filter.lookup_expr,
            label=value_filter._label,
            method=value_filter._method,
            distinct=value_filter.distinct,
            exclude=value_filter.exclude,
            **value_filter.extra,  # includes 'required'
            # Pass as **extra to the widget:
            subfield=value_filter.field,
        )
        self.value_filter = value_filter
        self.operator = operator

    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs
        if self.distinct:
            qs = qs.distinct()

        lookup = f"{self.field_name}__{self.lookup_expr}"
        op = self.OPERATORS[self.operator]
        q = reduce(op, (Q(**{lookup: subvalue}) for subvalue in value))
        return self.get_method(qs)(q)


class RangeFilter(filters.CharFilter):
    """Filter by effective date."""

    filter_name = "inWerkingOp"
    label = "Filter values effective on provided date/time."

    def __init__(self, start_field, end_field, lookup_expr="exact", **kwargs):
        self.start_field = self.convert_field_name(start_field)
        self.end_field = self.convert_field_name(end_field)
        super().__init__(field_name=self.start_field, lookup_expr=lookup_expr, **kwargs)

    def filter(self, qs, value):
        if value.strip() == "":
            return qs
        return qs.filter(
            (
                Q(**{f"{self.start_field}__lte": value})
                | Q(**{f"{self.start_field}__isnull": True})
            )
            & (
                Q(**{f"{self.end_field}__gt": value})
                | Q(**{f"{self.end_field}__isnull": True})
            )
        )

    def convert_field_name(self, field_name):
        if "." in field_name:
            return "__".join(
                [self.convert_field_name(part) for part in field_name.split(".")]
            )
        return to_snake_case(field_name)


class ModelIdChoiceField(fields.ModelChoiceField):
    """Allow testing an IN query against invalid ID's"""

    def to_python(self, value):
        """Bypass the queryset value check entirely.
        Copied the parts of the base method that are relevent here.
        """
        if self.null_label is not None and value == self.null_value:
            return value
        if value in self.empty_values:
            return None

        if isinstance(value, self.queryset.model):
            value = getattr(value, self.to_field_name or "pk")

        return value


class ModelIdChoiceFilter(filters.ModelChoiceFilter):
    """Improved choice filter for IN queries.

    Note that the django-filter's ``BaseFilterSet.filter_for_lookup()``
    subclasses this class as ``ConcreteInFilter(BaseInFilter, filter_class)``
    for lookup_type="in".
    """

    field_class = ModelIdChoiceField


class FlexDateTimeField(fields.IsoDateTimeField):
    """Allow input both as date or full datetime"""

    default_error_messages = {
        "invalid": _("Enter a valid ISO date-time, or single date."),
    }

    @cached_property
    def input_formats(self):
        # Note these types are lazy, hence the casts
        return list(fields.IsoDateTimeField.input_formats) + list(
            filters.DateFilter.field_class.input_formats
        )

    def strptime(self, value, format):
        if format in set(filters.DateFilter.field_class.input_formats):
            # Emulate forms.DateField.strptime()
            return datetime.strptime(value, format).date()
        else:
            return super().strptime(value, format)


class FlexDateTimeFilter(filters.IsoDateTimeFilter):
    """Flexible input parsing for a datetime field, allowing dates only."""

    field_class = FlexDateTimeField

    def filter(self, qs, value):
        """Implement filtering on single day for a 'datetime' field."""
        if value in EMPTY_VALUES:
            return qs
        if self.distinct:
            qs = qs.distinct()

        if not isinstance(value, datetime):
            # When something different then a full datetime is given, only compare dates.
            # Otherwise, the "lte" comparison happens against 00:00:00.000 of that date,
            # instead of anything that includes that day itself.
            lookup = f"date__{self.lookup_expr}"
        else:
            lookup = self.lookup_expr

        return self.get_method(qs)(**{f"{self.field_name}__{lookup}": value})


class CharArrayField(forms.CharField):
    """Comma separated strings field"""

    default_error_messages = {
        "invalid_choice": _(
            "Select a valid choice. %(value)s is not one of the available choices."
        ),
        "invalid_list": _("Enter a list of values."),
    }

    def to_python(self, value):
        if not value:
            value = []
        elif isinstance(value, str):
            value = value.split(",")
        elif not isinstance(value, (list, tuple)):
            raise ValidationError(
                self.error_messages["invalid_list"], code="invalid_list"
            )
        return [str(val) for val in value]


class CharArrayFilter(filters.BaseCSVFilter, filters.CharFilter):
    """Comma Separated Array filter"""

    base_field_class = CharArrayField


class DSOFilterSet(FilterSet):
    """Base class to create filter sets.

    The 'FILTER_DEFAULTS' field defines how fields are constructed.
    Usage in views::

        class MyFilterSet(DSOFilterSetBackend):
            class Meta:
                model = MyModel
                fields = {
                    'field1': ['exact', 'gt', 'lt', 'lte'],
                    'field2': ['exact'],
                }


        class View(APIView):
            filter_backends = [filters.DSOFilterSetBackend]
            filterset_class = MyFilterSet
    """

    FILTER_DEFAULTS = {
        **FilterSet.FILTER_DEFAULTS,
        # Unlike **GeoFilterSet.GEOFILTER_FOR_DBFIELD_DEFAULTS,
        # also enforce the geom_type for the input:
        models.CharField: {"filter_class": WildcardCharFilter},
        models.TextField: {"filter_class": WildcardCharFilter},
        models.DateTimeField: {
            # Only allow filtering on dates for now, ignore time component.
            "filter_class": FlexDateTimeFilter,
        },
        gis_models.GeometryField: {"filter_class": GeometryFilter,},
        # Unlike the base class, don't enforce ID value checking on foreign keys
        models.ForeignKey: {
            **FilterSet.FILTER_DEFAULTS[models.ForeignKey],
            "filter_class": ModelIdChoiceFilter,
        },
        models.OneToOneField: {
            **FilterSet.FILTER_DEFAULTS[models.OneToOneField],
            "filter_class": ModelIdChoiceFilter,
        },
        models.OneToOneRel: {
            **FilterSet.FILTER_DEFAULTS[models.OneToOneRel],
            "filter_class": ModelIdChoiceFilter,
        },
        UnlimitedCharField: {"filter_class": WildcardCharFilter},
        ArrayField: {"filter_class": CharArrayFilter},
    }

    FILTER_HELP_TEXT = {
        filters.BooleanFilter: "true | false",
        filters.CharFilter: "text",
        WildcardCharFilter: "text with wildcards",
        filters.DateFilter: "yyyy-mm-dd",
        FlexDateTimeFilter: "yyyy-mm-dd or yyyy-mm-ddThh:mm[:ss[.ms]]",
        filters.IsoDateTimeFilter: "yyyy-mm-ddThh:mm[:ss[.ms]]",
        filters.ModelChoiceFilter: "id",
        ModelIdChoiceFilter: "id",
        GeometryFilter: "GeoJSON | GEOMETRY(...)",
        CharArrayFilter: "Comma separated list of strings",
    }

    @classmethod
    def get_filter_name(cls, field_name, lookup_expr):
        """Generate the lookup expression syntax field[..]=..."""
        if lookup_expr == "exact":
            return field_name
        else:
            return f"{field_name}[{lookup_expr}]"

    @classmethod
    def filter_for_field(cls, field, field_name, lookup_expr="exact"):
        """Wrap the NOT filter in a multiple selector"""
        filter_instance = super().filter_for_field(
            field, field_name, lookup_expr=lookup_expr
        )

        if lookup_expr == "not":
            # Allow &field[not]=...&field[not]=...
            filter_instance = MultipleValueFilter(filter_instance)

        return filter_instance

    @classmethod
    def filter_for_lookup(cls, field, lookup_type):
        """Generate the 'label' if the model field doesn't present this.
        This data is shown in the Swagger docs, and browsable API.
        """
        filter_class, params = super().filter_for_lookup(field, lookup_type)
        if lookup_type == "isempty":
            filter_class = filters.BooleanFilter
        if filter_class is not None and "label" not in params:
            # description for swagger:
            params["label"] = cls.get_filter_help_text(
                filter_class, lookup_type, params
            )

        return filter_class, params

    @classmethod
    def get_filter_help_text(
        cls, filter_class: Type[filters.Filter], lookup_type, params
    ) -> str:
        """Get a brief default description for a filter in the API docs"""
        if issubclass(filter_class, GeometryFilter):
            geom_type = params.get("geom_type", "GEOMETRY")
            if lookup_type == "contains":
                help = "x,y | POINT(x y)"
            else:
                help = f"GeoJSON | {geom_type}(x y ...)"
            return help
        elif issubclass(filter_class, filters.BaseInFilter):
            # Auto-generated "ConcreteInFilter" class, e.g. ModelIdChoiceFilterIn
            if issubclass(filter_class, filters.ModelChoiceFilter):
                return "id1,id2,...,idN"

        try:
            return cls.FILTER_HELP_TEXT[filter_class]
        except KeyError:
            return filter_class.__name__.replace("Filter", "").lower()


class DSOFilterSetBackend(DjangoFilterBackend):
    """DSF fields filter.

    This loads the filterset logic of django-filter.
    Usage in views::

        class View(APIView):
            filter_backends = [filters.DSOFilterSetBackend]
            filterset_class = ... # subclass of DSOFilterSetBackend

    The ``filterset_class`` defines how each querystring field is parsed
    and processed.
    """

    filterset_base = DSOFilterSet

    def to_html(self, request, queryset, view):
        """See https://github.com/tomchristie/django-rest-framework/issues/3766.

        This prevents DRF from generating the filter dropdowns
        (which can be HUGE in our case)
        """
        return ""

    def get_filterset(self, request, queryset, view):
        filterset = super().get_filterset(request, queryset, view)
        for name, value in filterset.data.items():
            if (
                name.endswith("[contains]")
                and name in filterset.base_filters
                and filterset.base_filters[name].__class__.__name__.endswith(
                    "GeometryFilter"
                )
            ):
                if value:
                    if m1 := re.match(
                        r"([-+]?\d*(?:\.\d+)?),([-+]?\d+(?:\.\d+)?)", value
                    ):
                        x = m1.group(1)
                        y = m1.group(2)
                    elif m1 := re.match(
                        r"POINT\(([-+]?\d+(?:\.\d+))? ([-+]?\d+(?:\.\d+))\)", value
                    ):
                        x = m1.group(1)
                        y = m1.group(2)
                    else:
                        continue
                    if x and y:
                        srid = request.accept_crs.srid if request.accept_crs else None
                        x_lon, y_lat, srid = _validate_convert_x_y(x, y, srid)
                        if srid in (4326, 28992) and (x_lon is None or y_lat is None):
                            raise ValueError(f"Invalid x,y values : {x},{y}")
                        # longitude, latitude for 4326 x,y otherwise
                        value = GEOSGeometry(f"POINT({x_lon} {y_lat})", srid)
                        new_data = filterset.data.copy()
                        new_data[name] = value
                        filterset.data = new_data

        return filterset


class DSOOrderingFilter(OrderingFilter):
    """DRF Ordering filter, following the DSO spec.
    Usage in views::

        class View(APIView):
            filter_backends = [filters.DSOOrderingFilter]

    This adds an ``?_sort=<fieldname>,-<desc-fieldname>`` option to the view.
    On the view, an ``view.ordering_fields`` attribute may limit which fields
    can be used in the sorting. By default, it's all serializer fields.
    """

    ordering_param = "_sort"

    def get_ordering(self, request, queryset, view):
        if self.ordering_param not in request.query_params:
            # Allow DSO 1.0 Dutch "sorteer" parameter
            # Can adjust 'self' as this instance is recreated each request.
            if "sorteer" in request.query_params:
                self.ordering_param = "sorteer"

        ordering = super().get_ordering(request, queryset, view)
        if ordering is None:
            return ordering

        # convert to snake_case, preserving `-` if needed
        correct_ordering = [
            "-".join([to_snake_case(y) for y in x.split("-")]) for x in ordering
        ]
        return correct_ordering

    def remove_invalid_fields(self, queryset, fields, view, request):
        """Raise errors for invalid parameters instead of silently dropping them."""
        cleaned = super().remove_invalid_fields(queryset, fields, view, request)
        if cleaned != fields:
            invalid = ", ".join(sorted(set(fields).difference(cleaned)))
            raise ValidationError(f"Invalid sort fields: {invalid}", code="order-by")
        return cleaned
