"""Test-only views exercising the module 0 building blocks
(mounted by api.tests.urls only, never in production; kept out of the schema)."""
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes, schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.common.fields import HashidField, HashidRelatedField
from api.common.pagination import StandardPagination
from api.common.permissions import HasAcceptedTerms, HasVerifiedEmail, IsOwner, IsVerifiedDriver
from api.common.throttles import check_rate_limit
from komute.models import CustomUser, LegalAcceptance


@api_view(['GET'])
@schema(None)
def whoami(request):
    return Response({'id': HashidField().to_representation(request.user.pk), 'email': request.user.email})


@api_view(['GET'])
@schema(None)
@permission_classes([IsAuthenticated, HasAcceptedTerms])
def terms_gate(request):
    return Response({'ok': True})


@api_view(['GET'])
@schema(None)
@permission_classes([IsAuthenticated, HasVerifiedEmail])
def email_gate(request):
    return Response({'ok': True})


@api_view(['GET'])
@schema(None)
@permission_classes([IsAuthenticated, IsVerifiedDriver])
def driver_gate(request):
    return Response({'ok': True})


class AcceptanceDetail(APIView):
    schema = None
    permission_classes = [IsAuthenticated, IsOwner]

    def get(self, request, hashid):
        pk = HashidField().run_validation(hashid)
        obj = get_object_or_404(LegalAcceptance, pk=pk)
        self.check_object_permissions(request, obj)
        return Response({'id': hashid})


class EchoSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=10)
    ref = HashidField()
    user = HashidRelatedField(queryset=CustomUser.objects.all())

    def validate(self, attrs):
        if attrs['name'] == 'clash':
            raise serializers.ValidationError('These values clash.')
        return attrs


@api_view(['POST'])
@schema(None)
def echo(request):
    serializer = EchoSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    return Response({'name': data['name'], 'ref_pk': data['ref'], 'user_pk': data['user'].pk,
                     'user': EchoSerializer().fields['user'].to_representation(data['user'])})


@api_view(['GET'])
@schema(None)
def numbers(request):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(list(range(1, 46)), request)
    return paginator.get_paginated_response(page)


@api_view(['GET'])
@schema(None)
def boom(request):
    raise RuntimeError('kaboom')


@api_view(['GET'])
@schema(None)
def django_404(request):
    get_object_or_404(CustomUser, pk=0)


@api_view(['POST'])
@schema(None)
def action_limited(request):
    check_rate_limit(request, 'contact')
    return Response({'ok': True})
