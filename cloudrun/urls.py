"""项目根路由，只负责挂载业务 API。"""

from django.urls import path, include
urlpatterns = [path("", include("api.urls"))]
