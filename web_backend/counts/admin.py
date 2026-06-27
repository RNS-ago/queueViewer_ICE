from django.contrib import admin, messages

from .models import ApiKey, CountRecord


@admin.register(CountRecord)
class CountRecordAdmin(admin.ModelAdmin):
    list_display = ("device_id", "event", "ts", "count_in", "count_out", "occupancy", "received_at")
    list_filter = ("device_id", "event", "boot_id")
    search_fields = ("device_id", "boot_id", "ts")
    date_hierarchy = "received_at"


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "prefix", "active", "created_at", "last_used_at")
    list_filter = ("active",)
    search_fields = ("name", "prefix")
    readonly_fields = ("prefix", "created_at", "last_used_at")

    def get_fields(self, request, obj=None):
        # On the "add" form only ask for a name (+ active); everything else is
        # generated. On the change form show the read-only details.
        if obj is None:
            return ("name", "active")
        return ("name", "prefix", "active", "created_at", "last_used_at")

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            return
        # New key: generate, store the hash, and reveal the raw key once.
        new_obj, raw = ApiKey.generate(obj.name)
        if not obj.active:
            new_obj.active = False
            new_obj.save(update_fields=["active"])
        # Point the admin at the saved row so the redirect/edit page works.
        obj.pk = new_obj.pk
        self.message_user(
            request,
            f"API key for “{new_obj.name}”:  {raw}  — copy it now, it will not be shown again.",
            level=messages.WARNING,
        )
