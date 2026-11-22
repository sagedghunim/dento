"""Clinic configuration routes: sections and sessions (design 6.2, D1, D2)."""

from django.urls import path

from . import team_views, views

urlpatterns = [
    path("api/v1/sections", views.sections_view),
    path("api/v1/sections/<int:section_id>/services", views.section_services_view),
    path("api/v1/sections/<int:section_id>/doctors", views.section_doctors_view),
    path("api/v1/schedules", views.schedules_endpoint),
    path("api/v1/schedules/<int:schedule_id>", views.schedule_endpoint),
    path("api/v1/sections/<int:section_id>", views.section_update_view),
    path("api/v1/settings/profile", views.profile_view),
    # Team (design G1, G2)
    path("api/v1/team", team_views.team_view),
    path("api/v1/team/members", team_views.team_members_endpoint),
    path("api/v1/team/members/<int:member_id>", team_views.member_update_view),
    path("api/v1/team/members/<int:member_id>/permissions", team_views.permissions_view),
    path("api/v1/team/members/<int:member_id>/hours", team_views.hours_view),
    path("api/v1/team/members/<int:member_id>/temporary-password", team_views.password_view),
    path("api/v1/team/members/<int:member_id>/places", team_views.member_places_endpoint),
    path("api/v1/me/invitations", team_views.my_invitations_view),
    path("api/v1/me/invitations/<int:invitation_id>/accept", team_views.accept_invitation_view),
]
