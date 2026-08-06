"""项目根路由，只负责挂载业务 API。"""

from django.urls import include, path

from api.ninja_api import api


urlpatterns = [
    path("", include("api.urls")),
    path("api/", api.urls),
]
