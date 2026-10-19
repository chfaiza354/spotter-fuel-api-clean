from django.db import models


class TruckStop(models.Model):
    """A truck stop from the OPIS fuel-price list.

    Latitude/longitude are filled in lazily (and cached) the first time a
    station is considered as a fuel-stop candidate, so the CSV import itself
    needs no network access.
    """
    opis_id = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128, db_index=True)
    state = models.CharField(max_length=8, db_index=True)
    rack_id = models.CharField(max_length=32, blank=True)
    retail_price = models.DecimalField(max_digits=8, decimal_places=4)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_failed = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["state", "retail_price"]),
            models.Index(fields=["city", "state", "retail_price"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}/gal"


class GeocodeCache(models.Model):
    """Caches Nominatim geocode / reverse-geocode results.

    Keeps external API calls to a minimum: each distinct place name is only
    ever looked up once. This is what lets the API answer quickly and stay
    within Nominatim's usage policy.
    """
    query = models.CharField(max_length=512, unique=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    display_name = models.CharField(max_length=512, blank=True)
    city = models.CharField(max_length=128, blank=True)
    state = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.query} -> ({self.latitude}, {self.longitude})"
