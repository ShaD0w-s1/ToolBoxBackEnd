"""项目根路由，只负责挂载业务 API。"""

from django.urls import include, path


urlpatterns = [
    path("", include("api.urls")),
]
