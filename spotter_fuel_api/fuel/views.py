import requests
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel.services import compute_route_plan


class RoutePlanView(APIView):
    """POST /api/route/ -> cheapest-fuel route plan.

    Request JSON:  {"start": "Chicago, IL", "finish": "Denver, CO"}
    Response JSON: start/finish geocodes, route (distance, duration, GeoJSON),
                   fuel_stops (cheapest stations + gallons + cost), totals.
    """

    def post(self, request):
        start = (request.data.get("start") or "").strip()
        finish = (request.data.get("finish") or "").strip()
        if not start or not finish:
            return Response(
                {"error": 'Both "start" and "finish" are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            plan = compute_route_plan(start, finish)
        except ValueError as exc:
            # Bad input: unknown place name or no route between them.
            return Response({"error": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        except requests.RequestException as exc:
            # Map/routing service unreachable.
            return Response(
                {"error": f"Routing service error: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(plan)
