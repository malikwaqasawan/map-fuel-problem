from django.db import models


class TruckStop(models.Model):
    """A fuel price row from fuel-prices-for-be-assessment.csv, enriched with coordinates.

    opis_id is not unique - the CSV has duplicate ids under different brand
    names at the same address (e.g. id 20 is both "PILOT TRAVEL CENTER #1243"
    and "PILOT #1243").
    """

    PRECISION_CHOICES = [
        ("address", "address"),
        ("city", "city"),
        ("none", "none"),
    ]
    SOURCE_CHOICES = [
        ("gazetteer", "gazetteer"),
        ("nominatim", "nominatim"),
        ("none", "none"),
    ]

    source_row_number = models.IntegerField(db_index=True)
    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100, db_index=True)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.CharField(max_length=20)
    retail_price = models.DecimalField(max_digits=12, decimal_places=8)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    coordinate_precision = models.CharField(max_length=10, choices=PRECISION_CHOICES, default="none")
    coordinate_source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="none")

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
            models.Index(fields=["city", "state"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
