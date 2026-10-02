from django.conf import settings
from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(required=False, allow_blank=False, trim_whitespace=True)
    finish = serializers.CharField(required=False, allow_blank=False, trim_whitespace=True)
    start_lat = serializers.FloatField(required=False)
    start_lon = serializers.FloatField(required=False)
    finish_lat = serializers.FloatField(required=False)
    finish_lon = serializers.FloatField(required=False)
    initial_fuel_gallons = serializers.FloatField(required=False)

    def validate_initial_fuel_gallons(self, value):
        if not (0 <= value <= settings.VEHICLE_TANK_CAPACITY_GAL):
            raise serializers.ValidationError(
                f"initial_fuel_gallons must be between 0 and {settings.VEHICLE_TANK_CAPACITY_GAL}"
            )
        return value

    def validate(self, data):
        has_start = bool(data.get("start")) or ("start_lat" in data and "start_lon" in data)
        has_finish = bool(data.get("finish")) or ("finish_lat" in data and "finish_lon" in data)
        if not has_start:
            raise serializers.ValidationError("Provide 'start' (a place name) or both 'start_lat'/'start_lon'.")
        if not has_finish:
            raise serializers.ValidationError("Provide 'finish' (a place name) or both 'finish_lat'/'finish_lon'.")
        if data.get("start") and data.get("finish") and data["start"].strip().lower() == data["finish"].strip().lower():
            raise serializers.ValidationError("'start' and 'finish' must be different locations.")
        return data
