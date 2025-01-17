# Copyright The Cloud Custodian Authors.
# SPDX-License-Identifier: Apache-2.0
import logging
import time
import json
import jmespath

from c7n.actions import ActionRegistry, BaseAction
from c7n.exceptions import PolicyValidationError
from c7n.filters import FilterRegistry, MetricsFilter
from c7n.manager import resources
from c7n.query import QueryResourceManager, TypeInfo
from c7n.utils import (
    local_session, type_schema, get_retry)
from c7n.tags import (
    TagDelayedAction, RemoveTag, TagActionFilter, Tag)
import c7n.filters.vpc as net_filters

filters = FilterRegistry('emr.filters')
actions = ActionRegistry('emr.actions')
log = logging.getLogger('custodian.emr')

filters.register('marked-for-op', TagActionFilter)


@resources.register('emr')
class EMRCluster(QueryResourceManager):
    """Resource manager for Elastic MapReduce clusters
    """

    class resource_type(TypeInfo):
        service = 'emr'
        arn_type = 'emr'
        permission_prefix = 'elasticmapreduce'
        cluster_states = ['WAITING', 'BOOTSTRAPPING', 'RUNNING', 'STARTING']
        enum_spec = ('list_clusters', 'Clusters', {'ClusterStates': cluster_states})
        name = 'Name'
        id = 'Id'
        date = "Status.Timeline.CreationDateTime"
        cfn_type = 'AWS::EMR::Cluster'

    action_registry = actions
    filter_registry = filters
    retry = staticmethod(get_retry(('ThrottlingException',)))

    def __init__(self, ctx, data):
        super(EMRCluster, self).__init__(ctx, data)
        self.queries = QueryFilter.parse(
            self.data.get('query', [
                {'ClusterStates': [
                    'running', 'bootstrapping', 'waiting']}]))

    @classmethod
    def get_permissions(cls):
        return ("elasticmapreduce:ListClusters",
                "elasticmapreduce:DescribeCluster")

    def get_resources(self, ids):
        # no filtering by id set supported at the api
        client = local_session(self.session_factory).client('emr')
        results = []
        for jid in ids:
            results.append(
                client.describe_cluster(ClusterId=jid)['Cluster'])
        return results

    def resources(self, query=None):
        q = self.consolidate_query_filter()
        if q is not None:
            query = query or {}
            for i in range(0, len(q)):
                query[q[i]['Name']] = q[i]['Values']
        return super(EMRCluster, self).resources(query=query)

    def consolidate_query_filter(self):
        result = []
        names = set()
        # allow same name to be specified multiple times and append the queries
        # under the same name
        for q in self.queries:
            query_filter = q.query()
            if query_filter['Name'] in names:
                for filt in result:
                    if query_filter['Name'] == filt['Name']:
                        filt['Values'].extend(query_filter['Values'])
            else:
                names.add(query_filter['Name'])
                result.append(query_filter)
        if 'ClusterStates' not in names:
            # include default query
            result.append(
                {
                    'Name': 'ClusterStates',
                    'Values': ['WAITING', 'RUNNING', 'BOOTSTRAPPING'],
                }
            )
        return result

    def augment(self, resources):
        client = local_session(
            self.get_resource_manager('emr').session_factory).client('emr')
        result = []
        # remap for cwmetrics
        for r in resources:
            cluster = self.retry(
                client.describe_cluster, ClusterId=r['Id'])['Cluster']
            result.append(cluster)
        return result


@EMRCluster.filter_registry.register('metrics')
class EMRMetrics(MetricsFilter):

    def get_dimensions(self, resource):
        # Job flow id is legacy name for cluster id
        return [{'Name': 'JobFlowId', 'Value': resource['Id']}]


@actions.register('mark-for-op')
class TagDelayedAction(TagDelayedAction):
    """Action to specify an action to occur at a later date

    :example:

    .. code-block:: yaml

            policies:
              - name: emr-mark-for-op
                resource: emr
                filters:
                  - "tag:Name": absent
                actions:
                  - type: mark-for-op
                    tag: custodian_cleanup
                    op: terminate
                    days: 4
                    msg: "Cluster does not have required tags"
    """


@actions.register('tag')
class TagTable(Tag):
    """Action to create tag(s) on a resource

    :example:

    .. code-block:: yaml

            policies:
              - name: emr-tag-table
                resource: emr
                filters:
                  - "tag:target-tag": absent
                actions:
                  - type: tag
                    key: target-tag
                    value: target-tag-value
    """

    permissions = ('elasticmapreduce:AddTags',)
    batch_size = 1
    retry = staticmethod(get_retry(('ThrottlingException',)))

    def process_resource_set(self, client, resources, tags):
        for r in resources:
            self.retry(client.add_tags, ResourceId=r['Id'], Tags=tags)


@actions.register('remove-tag')
class UntagTable(RemoveTag):
    """Action to remove tag(s) on a resource

    :example:

    .. code-block:: yaml

            policies:
              - name: emr-remove-tag
                resource: emr
                filters:
                  - "tag:target-tag": present
                actions:
                  - type: remove-tag
                    tags: ["target-tag"]
    """

    concurrency = 2
    batch_size = 5
    permissions = ('elasticmapreduce:RemoveTags',)

    def process_resource_set(self, client, resources, tag_keys):
        for r in resources:
            client.remove_tags(ResourceId=r['Id'], TagKeys=tag_keys)


@actions.register('terminate')
class Terminate(BaseAction):
    """Action to terminate EMR cluster(s)

    It is recommended to apply a filter to the terminate action to avoid
    termination of all EMR clusters

    :example:

    .. code-block:: yaml

            policies:
              - name: emr-terminate
                resource: emr
                query:
                  - ClusterStates: [STARTING, BOOTSTRAPPING, RUNNING, WAITING]
                actions:
                  - terminate
    """

    schema = type_schema('terminate', force={'type': 'boolean'})
    permissions = ("elasticmapreduce:TerminateJobFlows",)
    delay = 5

    def process(self, emrs):
        client = local_session(self.manager.session_factory).client('emr')
        cluster_ids = [emr['Id'] for emr in emrs]
        if self.data.get('force'):
            client.set_termination_protection(
                JobFlowIds=cluster_ids, TerminationProtected=False)
            time.sleep(self.delay)
        client.terminate_job_flows(JobFlowIds=cluster_ids)
        self.log.info("Deleted emrs: %s", cluster_ids)
        return emrs


# Valid EMR Query Filters
EMR_VALID_FILTERS = {'CreatedAfter', 'CreatedBefore', 'ClusterStates'}


class QueryFilter:

    @classmethod
    def parse(cls, data):
        results = []
        for d in data:
            if not isinstance(d, dict):
                raise PolicyValidationError(
                    "EMR Query Filter Invalid structure %s" % d)
            results.append(cls(d).validate())
        return results

    def __init__(self, data):
        self.data = data
        self.key = None
        self.value = None

    def validate(self):
        if not len(list(self.data.keys())) == 1:
            raise PolicyValidationError(
                "EMR Query Filter Invalid %s" % self.data)
        self.key = list(self.data.keys())[0]
        self.value = list(self.data.values())[0]

        if self.key not in EMR_VALID_FILTERS and not self.key.startswith(
                'tag:'):
            raise PolicyValidationError(
                "EMR Query Filter invalid filter name %s" % (self.data))

        if self.value is None:
            raise PolicyValidationError(
                "EMR Query Filters must have a value, use tag-key"
                " w/ tag name as value for tag present checks"
                " %s" % self.data)
        return self

    def query(self):
        value = self.value
        if isinstance(self.value, str):
            value = [self.value]

        return {'Name': self.key, 'Values': value}


@filters.register('subnet')
class SubnetFilter(net_filters.SubnetFilter):

    RelatedIdsExpression = "Ec2InstanceAttributes.RequestedEc2SubnetIds[]"


@filters.register('security-group')
class SecurityGroupFilter(net_filters.SecurityGroupFilter):

    RelatedIdsExpression = ""
    expressions = ('Ec2InstanceAttributes.EmrManagedMasterSecurityGroup',
                'Ec2InstanceAttributes.EmrManagedSlaveSecurityGroup',
                'Ec2InstanceAttributes.ServiceAccessSecurityGroup',
                'Ec2InstanceAttributes.AdditionalMasterSecurityGroups[]',
                'Ec2InstanceAttributes.AdditionalSlaveSecurityGroups[]')

    def get_related_ids(self, resources):
        sg_ids = set()
        for r in resources:
            for exp in self.expressions:
                ids = jmespath.search(exp, r)
                if isinstance(ids, list):
                    sg_ids.update(tuple(ids))
                elif isinstance(ids, str):
                    sg_ids.add(ids)
        return list(sg_ids)


filters.register('network-location', net_filters.NetworkLocation)


@resources.register('emr-security-configuration')
class EMRSecurityConfiguration(QueryResourceManager):
    """Resource manager for EMR Security Configuration
    """

    class resource_type(TypeInfo):
        service = 'emr'
        arn_type = 'emr'
        permission_prefix = 'elasticmapreduce'
        enum_spec = ('list_security_configurations', 'SecurityConfigurations', None)
        detail_spec = ('describe_security_configuration', 'Name', 'Name', None, None)
        id = name = 'Name'
        cfn_type = 'AWS::EMR::SecurityConfiguration'

    permissions = ('elasticmapreduce:ListSecurityConfigurations',
                  'elasticmapreduce:DescribeSecurityConfiguration',)

    def augment(self, resources):
        resources = super().augment(resources)
        for r in resources:
            r['SecurityConfiguration'] = json.loads(r['SecurityConfiguration'])
        return resources


@EMRSecurityConfiguration.action_registry.register('delete')
class DeleteEMRSecurityConfiguration(BaseAction):

    schema = type_schema('delete')
    permissions = ('elasticmapreduce:DeleteSecurityConfiguration',)

    def process(self, resources):
        client = local_session(self.manager.session_factory).client('emr')
        for r in resources:
            try:
                client.delete_security_configuration(Name=r['Name'])
            except client.exceptions.EntityNotFoundException:
                continue
