from django.contrib import admin

from routing.models import TruckStop


@admin.register(TruckStop)
class TruckStopAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "retail_price", "coordinate_precision", "coordinate_source")
    list_filter = ("state", "coordinate_precision", "coordinate_source")
    search_fields = ("name", "city", "address", "opis_id")
