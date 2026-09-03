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
    path("api/aircraft-info/", views.aircraft_info),
    path("api/control-docs/", views.control_docs),
    path("api/control-docs/<str:doc_id>/", views.control_doc_detail),
    path("api/prep-attachments/", views.prep_attachment_file),
    path("api/identity/", views.identity),
    path("api/identity/accounts/", views.accounts),
    path("api/online-count/", views.online_count),
    path("api/editing/", views.editing),
    path("api/eng-templates/", views.eng_templates),
    path("api/eng-templates/<str:template_id>/duplicate/", views.eng_template_duplicate),
    path("api/eng-templates/<str:template_id>/", views.eng_template_detail),
    path("api/standalone-templates/", views.standalone_templates),
    path("api/standalone-templates/<str:template_id>/duplicate/", views.standalone_template_duplicate),
    path("api/standalone-templates/<str:template_id>/", views.standalone_template_detail),
]
