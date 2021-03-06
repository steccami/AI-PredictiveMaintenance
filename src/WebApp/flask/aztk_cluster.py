import sys, os, time, glob
import aztk.models
import aztk.spark
import json
import azure.batch.models as batch_models
from aztk.spark.models import ClusterConfiguration, UserConfiguration
from azure.batch.models import BatchErrorException
from pprint import pprint
from aztk.error import AztkError
from azure.storage.table import TableService, Entity, TablePermissions

class ClusterStatus:
    NotCreated, Provisioning, Provisioned, Deleted, Failed, DeletionFailed = range(6)

class AztkCluster:
    def __init__(self, vm_count = 0, sku_type = 'standard_d2_v2', username = 'admin', password = 'admin'):
        self.vm_count = int(vm_count)
        self.sku_type = sku_type
        self.username = username
        self.password = password
        self.BATCH_ACCOUNT_NAME = os.environ['BATCH_ACCOUNT_NAME']
        BATCH_ACCOUNT_KEY = os.environ['BATCH_ACCOUNT_KEY']
        BATCH_SERVICE_URL = os.environ['BATCH_ACCOUNT_URL']
        STORAGE_ACCOUNT_SUFFIX = 'core.windows.net'
        self.STORAGE_ACCOUNT_NAME = os.environ['STORAGE_ACCOUNT_NAME']
        self.STORAGE_ACCOUNT_KEY = os.environ['STORAGE_ACCOUNT_KEY']

        self.secrets_config = aztk.spark.models.SecretsConfiguration(
            shared_key=aztk.models.SharedKeyConfiguration(
            batch_account_name = self.BATCH_ACCOUNT_NAME,
            batch_account_key = BATCH_ACCOUNT_KEY,
            batch_service_url = BATCH_SERVICE_URL,
            storage_account_name = self.STORAGE_ACCOUNT_NAME,
            storage_account_key = self.STORAGE_ACCOUNT_KEY,
            storage_account_suffix = STORAGE_ACCOUNT_SUFFIX
            ),

        ssh_pub_key=""
        )
        self.table_service = TableService(account_name=self.STORAGE_ACCOUNT_NAME, account_key=self.STORAGE_ACCOUNT_KEY)

    def createCluster(self):


        # create a client
        client = aztk.spark.Client(self.secrets_config)

        # list available clusters
        clusters = client.list_clusters()

        SPARK_CONFIG_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), 'spark', 'spark', '.config'))
        SPARK_JARS_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), 'spark','spark', 'jars'))

        SPARK_CORE_SITE = os.path.join(SPARK_CONFIG_PATH, 'core-site.xml')

        jars = glob.glob(os.path.join(SPARK_JARS_PATH, '*.jar'))

        # define spark configuration
        spark_conf = aztk.spark.models.SparkConfiguration(
            spark_defaults_conf=os.path.join(SPARK_CONFIG_PATH, 'spark-defaults.conf'),
            spark_env_sh=os.path.join(SPARK_CONFIG_PATH, 'spark-env.sh'),
            core_site_xml=SPARK_CORE_SITE,
            jars=jars
        )

        clusterDetails = self.table_service.get_entity('cluster', 'predictivemaintenance', 'predictivemaintenance')
        cluster_number = int(clusterDetails.ClusterNumber) + 1
        cluster_id = clusterDetails.PartitionKey + str(cluster_number)

        jupyterCustomScript = aztk.models.CustomScript("jupyter", "D:/home/site/wwwroot/flask/spark/customScripts/jupyter.sh", "all-nodes")
        azuremlProjectFileShare = aztk.models.FileShare(self.STORAGE_ACCOUNT_NAME, self.STORAGE_ACCOUNT_KEY, 'azureml-project', '/mnt/azureml-project')
        azuremlFileShare = aztk.models.FileShare(self.STORAGE_ACCOUNT_NAME, self.STORAGE_ACCOUNT_KEY, 'azureml-share', '/mnt/azureml-share')
        # configure my cluster
        cluster_config = aztk.spark.models.ClusterConfiguration(
            docker_repo='aztk/python:spark2.2.0-python3.6.2-base',
            cluster_id= cluster_id, # Warning: this name must be a valid Azure Blob Storage container name
            vm_count=self.vm_count,
            # vm_low_pri_count=2, #this and vm_count are mutually exclusive
            vm_size=self.sku_type,
            custom_scripts=[jupyterCustomScript],
            spark_configuration=spark_conf,
            file_shares=[azuremlProjectFileShare, azuremlFileShare],
            user_configuration=UserConfiguration(
                username=self.username,
                password=self.password,
            )
        )
        try:
            cluster = client.create_cluster(cluster_config)
        except Exception as e:
            clusterDetails = {'PartitionKey': 'predictivemaintenance', 'RowKey': 'predictivemaintenance', 'Status': ClusterStatus.Failed, 'UserName': self.username,'ClusterNumber': cluster_number,'Message': str(e)}
            self.table_service.insert_or_merge_entity('cluster', clusterDetails)
            return

        clusterDetails = {'PartitionKey': 'predictivemaintenance', 'RowKey': 'predictivemaintenance', 'Status': ClusterStatus.Provisioning, 'UserName': self.username,'ClusterNumber': cluster_number}
        self.table_service.insert_or_merge_entity('cluster', clusterDetails)

    def getCluster(self):
        # create a client
        client = aztk.spark.Client(self.secrets_config)
        try:
            clusterDetails = self.table_service.get_entity('cluster', 'predictivemaintenance', 'predictivemaintenance')
        except Exception as e:
            clusterDetails = {'PartitionKey': 'predictivemaintenance', 'RowKey': 'predictivemaintenance', 'Status': ClusterStatus.NotCreated , 'ClusterNumber': '0'}
            self.table_service.insert_or_merge_entity('cluster', clusterDetails)
            return clusterDetails

        cluster_id = clusterDetails.PartitionKey + str(clusterDetails.ClusterNumber)
        if clusterDetails.Status == ClusterStatus.Deleted or clusterDetails.Status == ClusterStatus.NotCreated:
            return clusterDetails
        try:
            cluster = client.get_cluster(cluster_id=cluster_id)
            for node in cluster.nodes:
                    remote_login_settings = client.get_remote_login_settings(cluster.id, node.id)
                    if node.state in [batch_models.ComputeNodeState.unknown, batch_models.ComputeNodeState.unusable, batch_models.ComputeNodeState.start_task_failed]:
                        errorMsg = "An error occured while starting the Nodes in the batch account " + self.BATCH_ACCOUNT_NAME + ". Details: "
                        if node.start_task_info.failure_info != None:
                            errorMsg += node.start_task_info.failure_info.message
                        clusterDetails = {'PartitionKey': 'predictivemaintenance', 'RowKey': 'predictivemaintenance', 'Status': ClusterStatus.Failed, 'UserName': self.username,'ClusterNumber': clusterDetails.ClusterNumber,'Message': errorMsg}
                        self.table_service.insert_or_merge_entity('cluster', clusterDetails)
                        return clusterDetails

                    if node.id == cluster.master_node_id:
                        master_ipaddress = remote_login_settings.ip_address
                        master_Port = remote_login_settings.port
                        clusterDetails = {'PartitionKey': 'predictivemaintenance', 'RowKey': 'predictivemaintenance', 'Status': ClusterStatus.Provisioned, 'Master_Ip_Address': master_ipaddress, 'Master_Port': master_Port, 'UserName': clusterDetails.UserName, 'ClusterNumber': clusterDetails.ClusterNumber}
                        self.table_service.insert_or_merge_entity('cluster', clusterDetails)
        except (AztkError, BatchErrorException):
            clusterDetails = self.table_service.get_entity('cluster', 'predictivemaintenance', 'predictivemaintenance')

        return clusterDetails

    def deleteCluster(self):


        # create a client
        client = aztk.spark.Client(self.secrets_config)
        clusterDetails = self.table_service.get_entity('cluster', 'predictivemaintenance', 'predictivemaintenance')
        cluster_id = clusterDetails.PartitionKey + str(clusterDetails.ClusterNumber)
        try:
            client.delete_cluster(cluster_id = cluster_id)
        except Exception as e:
            clusterDetails = {'PartitionKey': 'predictivemaintenance', 'RowKey': 'predictivemaintenance', 'Status': ClusterStatus.DeletionFailed, 'UserName': self.username, 'ClusterNumber': clusterDetails.ClusterNumber,'Message': str(e)}
            self.table_service.insert_or_merge_entity('cluster', clusterDetails)
            return

        clusterDetails = {'PartitionKey': 'predictivemaintenance', 'RowKey': 'predictivemaintenance', 'Status': ClusterStatus.Deleted, 'ClusterNumber': clusterDetails.ClusterNumber}
        self.table_service.insert_or_merge_entity('cluster', clusterDetails)

if __name__ == '__main__':
    pass