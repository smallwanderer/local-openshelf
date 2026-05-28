"""URL patterns for the Sync API."""

from django.urls import path

from . import views

app_name = "sync_api"

urlpatterns = [
    path("v1/ping/", views.ping, name="ping"),
    path("v1/diff/", views.diff, name="diff"),
    path("v1/upload/", views.upload, name="upload"),
    path("v1/mkdir/", views.mkdir, name="mkdir"),
    path("v1/delete/", views.delete, name="delete"),
    path("v1/confirm/", views.confirm, name="confirm"),
]
