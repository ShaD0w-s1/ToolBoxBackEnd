from django.urls import path

from . import views

urlpatterns = [
    path("", views.index),
    path("api/csrf/", views.csrf),
    path("api/cloudbase/status/", views.cloudbase_status),
    path("api/projects/", views.projects),
    path("api/projects/<str:project_id>/", views.project_detail),
    path("api/templates/<str:aircraft_type>/", views.aircraft_template),
    path("api/tool-cart/", views.tool_cart),
]
