"""URL configuration for spotter_fuel_api project."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('fuel.urls')),
]
