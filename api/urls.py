"""业务 API 路由表；所有公开接口统一保留在 /api 前缀下。"""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.index),
    path("api/csrf/", views.csrf),
    path("api/airnav-verify/", views.airnav_verify),
    path("api/cloudbase/status/", views.cloudbase_status),
    path("api/poll/", views.poll),
    path("api/projects/", views.projects),
    path("api/projects/<str:project_id>/", views.project_detail),
    path("api/projects/<str:project_id>/apply-workcard/", views.apply_workcard),
    path("api/templates/<str:aircraft_type>/", views.aircraft_template),
    path("api/material-templates/<str:aircraft_type>/", views.material_template),
    path("api/tool-cart/", views.tool_cart),
    path("api/announcement/", views.announcement),
    path("api/standard-libraries/<str:lib_key>/", views.standard_library),
    path("api/config/", views.app_config),
    path("api/aircraft-numbers/", views.aircraft_numbers),
    path("api/control-docs/", views.control_docs),
    path("api/control-docs/<str:doc_id>/", views.control_doc_detail),
]
