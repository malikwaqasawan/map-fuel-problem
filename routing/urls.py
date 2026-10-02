from django.urls import path

from routing import views

urlpatterns = [
    path("api/route/", views.RoutePlanView.as_view(), name="route-plan"),
    path("map/<str:result_key>/", views.map_view, name="route-map"),
]
