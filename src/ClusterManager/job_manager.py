#!/usr/bin/python 
# -*- coding: UTF-8 -*-

import json
import os
import time
import argparse
import uuid
import subprocess
import sys
import datetime
import copy


sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),"../storage"))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),"../utils"))

from jobs_tensorboard import GenTensorboardMeta
import k8sUtils
import joblog_manager
from osUtils import mkdirsAsUser

import yaml
from jinja2 import Environment, FileSystemLoader, Template
from config import config, GetStoragePath, GetWorkPath
from DataHandler import DataHandler
from node_manager import create_log
from node_manager import get_cluster_status
import base64

import re

import thread
import threading
import random

import logging
import logging.config


nvidiaDriverPath = config["nvidiaDriverPath"] if "nvidiaDriverPath" in config else "/usr/local/cuda/lib64"


def printlog(msg):
    log_data =  "%s - %s" % (datetime.datetime.utcnow().strftime("%x %X"), msg)
    logging.info(log_data)
    return

def LoadJobParams(jobParamsJsonStr):
    return json.loads(jobParamsJsonStr)

def cmd_exec(cmdStr):
    try:
        output = subprocess.check_output(["bash","-c", cmdStr])
        logging.info("bash -c " + cmdStr)
        logging.info("output: " + output)

    except Exception as e:
        print e
        output = ""
        logging.info(str(e))

    return output


def SubmitJob(job):
    jobParams = json.loads(base64.b64decode(job["jobParams"]))

    if jobParams["jobtrainingtype"] == "RegularJob":
        SubmitRegularJob(job)

    elif jobParams["jobtrainingtype"] == "PSDistJob":
        SubmitPSDistJob(job)

    return

def CheckMountPoints(mplist, mp):
    ret = True
    for item in mplist:
        if item["name"] == mp["name"] or item["containerPath"] == mp["containerPath"] or item["hostPath"] == mp["hostPath"]:
            ret = False

    return ret

def SubmitRegularJob(job):
    ret = {}
    dataHandler = DataHandler()
    logging.info("start to submit regular job...")

    try:
        jobParams = json.loads(base64.b64decode(job["jobParams"]))

        jobParams["pvc_job"] = "jobs-" + jobParams["jobId"]
        jobParams["pvc_work"] = "work-" + jobParams["jobId"]
        jobParams["pvc_data"] = "storage-" + jobParams["jobId"]


        if "jobPath" not in jobParams or len(jobParams["jobPath"].strip()) == 0: 
            dataHandler.SetJobError(jobParams["jobId"],"ERROR: job-path does not exist")
            msg = "ERROR: job-path does not exist. jobid: %s" % (jobParams["jobId"])
            logging.error(msg)
            return False

        if "workPath" not in jobParams or len(jobParams["workPath"].strip()) == 0: 
            dataHandler.SetJobError(jobParams["jobId"],"ERROR: work-path does not exist")

            msg = "ERROR: work-path does not exist. jobid: %s" % (jobParams["jobId"])
            logging.error(msg)
            return False

        #if "dataPath" not in jobParams or len(jobParams["dataPath"].strip()) == 0: 
        #    dataHandler.SetJobError(jobParams["jobId"],"ERROR: data-path does not exist")
        #    return False
        jobPath, workPath, dataPath = GetStoragePath(jobParams["jobPath"],jobParams["workPath"],jobParams["dataPath"])
        localJobPath = os.path.join(config["storage-mount-path"],jobPath)

        if not os.path.exists(localJobPath):
            if "userId" in jobParams:
                mkdirsAsUser(localJobPath,jobParams["userId"])
                mkdirsAsUser(os.path.join(localJobPath,"models"),jobParams["userId"])
            else:
                mkdirsAsUser(localJobPath,"0")
                mkdirsAsUser(os.path.join(localJobPath,"models"),"0")

        jobParams["LaunchCMD"] = ""
        if "cmd" not in jobParams:
            jobParams["cmd"] = ""
            
        if isinstance(jobParams["cmd"], basestring) and not jobParams["cmd"] == "":
            launchScriptPath = os.path.join(localJobPath,"launch-%s.sh" % jobParams["jobId"])

            with open(launchScriptPath, 'w') as f:
                f.write("#!/bin/bash -x\n")
                f.write(jobParams["cmd"] + "\n")

                msg = "write cmd(%s) to file: %s" % (jobParams["cmd"], launchScriptPath)
                logging.info(msg)

            f.close()    
            if "userId" in jobParams:
                cmd = "chown -R %s %s" % (jobParams["userId"], launchScriptPath)
                os.system(cmd)
                logging.info(cmd)

            # todo: Pod启动后会执行shell脚本，需预先将shell脚本拷贝到Pod所在的节点机器的目录：
            # 譬如：/dlwsdata/work/user-nanme/jobs/191225/6f81459e-42ea-447e-9380-f545da2517e9/ 
            # Pod启动后，会将此目录挂载至/job/
            # jobParams["LaunchCMD"] = "[\"bash\", \"/job/launch-%s.sh\"]" % jobParams["jobId"]
            jobParams["LaunchCMD"] = "[\"/bin/sh\", \"-ec\", \"sleep 6000315360000\"]"

        jobParams["jobDescriptionPath"] = "jobfiles/" + time.strftime("%y%m%d") + "/" + jobParams["jobId"] + "/" + jobParams["jobId"] + ".yaml"
        jobParams["jobNameLabel"] = ''.join(e for e in jobParams["jobName"] if e.isalnum())
        ENV = Environment(loader=FileSystemLoader("/"))

        jobTempDir = os.path.join(config["root-path"],"Jobs_Templete")
        jobTemp = os.path.join(jobTempDir, "RegularJob.yaml.template")

        jobParams["hostjobPath"] = os.path.join(config["storage-mount-path"], jobPath)
        jobParams["hostworkPath"] = os.path.join(config["storage-mount-path"], workPath)
        jobParams["hostdataPath"] = os.path.join(config["storage-mount-path"], dataPath)
        jobParams["nvidiaDriverPath"] = nvidiaDriverPath

        jobParams["userNameLabel"] = getAlias(jobParams["userName"])
        jobParams["rest-api"] = config["rest-api"]

        if "mountpoints" not in jobParams:
            jobParams["mountpoints"] = []

        for onemount in jobParams["mountpoints"]:
            onemount["name"] = onemount["containerPath"].replace("/","").replace(".","").replace("_","-")

        # mp = {"name":"nvidia-driver","containerPath":"/usr/local/nvidia","hostPath":nvidiaDriverPath, "enabled":True}
        # if CheckMountPoints(jobParams["mountpoints"],mp):
        #    jobParams["mountpoints"].append(mp)

        mp = {"name":"job","containerPath":"/job","hostPath":jobParams["hostjobPath"], "enabled":True}
        if CheckMountPoints(jobParams["mountpoints"],mp):
            jobParams["mountpoints"].append(mp)

        mp = {"name":"work","containerPath":"/work","hostPath":jobParams["hostworkPath"], "enabled":True}
        if CheckMountPoints(jobParams["mountpoints"],mp):
            jobParams["mountpoints"].append(mp)

        mp = {"name":"data","containerPath":"/data","hostPath":jobParams["hostdataPath"], "enabled":True}
        if CheckMountPoints(jobParams["mountpoints"],mp):
            jobParams["mountpoints"].append(mp)                        

        userAlias = getAlias(jobParams["userName"])

        mp = {"name":"sshkey","containerPath":"/home/%s/.ssh" % userAlias,"hostPath":os.path.join(config["storage-mount-path"], GetWorkPath(userAlias)+"/.ssh"), "readOnly":True, "enabled":True} #  
        if CheckMountPoints(jobParams["mountpoints"],mp):
            jobParams["mountpoints"].append(mp)            

        jobParams["pod_ip_range"] = config["pod_ip_range"]
        if "usefreeflow" in config:
            jobParams["usefreeflow"] = config["usefreeflow"]
        else:
            jobParams["usefreeflow"] = False

        msg = ("Render Job: %s" % jobParams)
        print (msg)
        logging.info(msg)

        jobDescriptionList = []
        pods = []

        if "hyperparametername" in jobParams and "hyperparameterstartvalue" in jobParams and "hyperparameterendvalue" in jobParams and "hyperparameterstep" in jobParams:
            i = int(jobParams["hyperparameterstartvalue"])
            end = int(jobParams["hyperparameterendvalue"])
            step = int(jobParams["hyperparameterstep"])
            c = 0

            while (i <= end):
                pod = {}
                pod["podName"] = jobParams["jobId"]+"-pod-"+str(c)
                pod["envs"] = [{"name":jobParams["hyperparametername"],"value":i}]
                i += step
                c += 1 
                pods.append(pod)
        else:
                pod = {}
                pod["podName"] = jobParams["jobId"]
                pod["envs"] = []
                pods.append(pod)

        if "env" not in jobParams:
            jobParams["env"] = []

        jobParams["commonenv"] = copy.copy(jobParams["env"])
        for pod in pods:
            jobParams["podName"] = pod["podName"]
            jobParams["env"] = jobParams["commonenv"] + pod["envs"]

            if "kube_custom_scheduler" in config and config["kube_custom_scheduler"]:
                container = {}
                container["requests"] = {"alpha.gpu/numgpu" : int(jobParams["resourcegpu"])}
                podInfo = {}
                podInfo["podname"] = jobParams["podName"]
                if "useGPUTopology" in jobParams and jobParams["useGPUTopology"]:
                    # add topology constraints explicitly - for testing
                    # if (jobParams["resourcegpu"] >= 2):
                    #     # both cards in same inner group
                    #     container["requests"]["alpha/grpresource/gpugrp1/0/gpugrp0/0/gpu/0/cards"] = 1
                    #     container["requests"]["alpha/grpresource/gpugrp1/0/gpugrp0/0/gpu/1/cards"] = 1
                    # if (jobParams["resourcegpu"] >= 3):
                    #     container["requests"]["alpha/grpresource/gpugrp1/0/gpugrp0/1/gpu/2/cards"] = 1
                    # if (jobParams["resourcegpu"] >= 4):
                    #     container["requests"]["alpha/grpresource/gpugrp1/0/gpugrp0/1/gpu/3/cards"] = 1
                    # if (jobParams["resourcegpu"] >= 5):
                    #     container["requests"]["alpha/grpresource/gpugrp1/1/gpugrp0/2/gpu/4/cards"] = 1
                    # if (jobParams["resourcegpu"] >= 6):
                    #     container["requests"]["alpha/grpresource/gpugrp1/1/gpugrp0/2/gpu/5/cards"] = 1
                    # if (jobParams["resourcegpu"] >= 7):
                    #     container["requests"]["alpha/grpresource/gpugrp1/1/gpugrp0/3/gpu/6/cards"] = 1
                    # if (jobParams["resourcegpu"] >= 8):
                    #     container["requests"]["alpha/grpresource/gpugrp1/1/gpugrp0/3/gpu/7/cards"] = 1
                    podInfo["requests"] = {"alpha.gpu/gpu-generate-topology" : 1}
                else:
                    # for cases when desired topology is explictly given or not desired
                    podInfo["requests"] = {"alpha.gpu/gpu-generate-topology" : 0}
                podInfo["runningcontainer"] = {jobParams["podName"] : container}

                if "annotations" not in jobParams:
                    jobParams["annotations"] = {}
                jobParams["annotations"]["pod.alpha/DeviceInformation"] = "'" + json.dumps(podInfo) + "'"
                jobParams["resourcegpu"] = 0 # gpu requests specified through annotation

            template = ENV.get_template(os.path.abspath(jobTemp))
            job_description = template.render(job=jobParams)
            jobDescriptionList.append(job_description)

            if ("interactivePort" in jobParams and len(jobParams["interactivePort"].strip()) > 0):
                ports = [p.strip() for p in re.split(",|;",jobParams["interactivePort"]) if len(p.strip()) > 0 and p.strip().isdigit()]
                for portNum in ports:
                    jobParams["serviceId"] = "interactive-" + jobParams["podName"] + "-" + portNum
                    jobParams["port"] = portNum
                    jobParams["port-name"] = "interactive"
                    jobParams["port-type"] = "TCP"

                    serviceTemplate = ENV.get_template(os.path.join(jobTempDir,"KubeSvc.yaml.template"))
                    stemplate = ENV.get_template(serviceTemplate)
                    interactiveMeta = stemplate.render(svc=jobParams)
                    jobDescriptionList.append(interactiveMeta)

        jobDescription = "\n---\n".join(jobDescriptionList)
        jobDescriptionPath = os.path.join(config["storage-mount-path"], jobParams["jobDescriptionPath"])

        if not os.path.exists(os.path.dirname(os.path.realpath(jobDescriptionPath))):
            os.makedirs(os.path.dirname(os.path.realpath(jobDescriptionPath)))

        if os.path.isfile(jobDescriptionPath):
            output = k8sUtils.kubectl_delete(jobDescriptionPath) 
            logging.info("kubectl delete " + jobDescriptionPath + " output: " + str(output))

        with open(jobDescriptionPath, 'w') as f:
            f.write(jobDescription)

        output = k8sUtils.kubectl_create(jobDescriptionPath)    
        logging.info("kubectl create " + jobDescriptionPath + " output: " + str(output))

        msg = "Submitted job %s to k8s, returned with status %s" %(jobParams["jobId"], output)
        logging.info(msg)

        msg = "JobParams: \n" + json.dumps(jobParams)
        logging.info(msg) 


        ## 启动命令非空
        if isinstance(jobParams["cmd"], basestring) and not jobParams["cmd"] == "":
            ## 等待docker启动完毕，再执行文件拷贝指令
            time.sleep(15) 
            launch_file_name = "launch-%s.sh" % jobParams["jobId"]

            # 将文件拷贝进podName:/tmp/
            # /job/目录需要root权限才能操作，因此此处无法直接拷贝进/job/
            remotecmd = "cp %s %s:%s" % (launchScriptPath, jobParams["podName"], "/tmp/")
            output = k8sUtils.kubectl_exec(remotecmd)
            logging.info("remotecmd[" + remotecmd + "]" + " output[" + str(output) + "]") 

            # 添加执行权限：/tmp/lunach_jobid.sh
            remotecmd = "exec %s -- bash -c \"chmod 777 /tmp/%s\"" % (jobParams["jobId"], launch_file_name)
            output = k8sUtils.kubectl_exec(remotecmd)
            logging.info("remotecmd[" + remotecmd + "]" + " output[" + str(output) + "]") 

            # 执行/tmp/lunach_jobid.sh
            remotecmd = "exec %s -- bash -c \"/tmp/%s\"" % (jobParams["jobId"], launch_file_name)
            output = k8sUtils.kubectl_exec(remotecmd)
            logging.info("remotecmd[" + remotecmd + "]" + " output[" + str(output) + "]") 

        else:
            pass


        ret["output"] = output
        ret["jobId"] = jobParams["jobId"]

        if "userName" not in jobParams:
            jobParams["userName"] = ""

        dataHandler.UpdateJobTextField(jobParams["jobId"],"jobStatus","scheduling")
        dataHandler.UpdateJobTextField(jobParams["jobId"],"jobDescriptionPath",jobParams["jobDescriptionPath"])
        dataHandler.UpdateJobTextField(jobParams["jobId"],"jobDescription",base64.b64encode(jobDescription))

        jobMeta = {}
        jobMeta["jobDescriptionPath"] = jobParams["jobDescriptionPath"]
        jobMeta["jobPath"] = jobParams["jobPath"]
        jobMeta["workPath"] = jobParams["workPath"]
        jobMeta["jobPath"] = jobParams["jobPath"]
        jobMeta["LaunchCMD"] = jobParams["LaunchCMD"]

        jobMetaStr = base64.b64encode(json.dumps(jobMeta))
        dataHandler.UpdateJobTextField(jobParams["jobId"],"jobMeta", jobMetaStr)

        msg = "update job text field %s, returned with status" % (jobParams["jobId"])
        logging.info(msg)

    except Exception as e:
        print e
        ret["error"] = str(e)
        retries = dataHandler.AddandGetJobRetries(jobParams["jobId"])

        if retries >= 5:
            dataHandler.UpdateJobTextField(jobParams["jobId"],"jobStatus","error")
            dataHandler.UpdateJobTextField(jobParams["jobId"],"errorMsg","Cannot submit job!" + str(e))

    return ret



def SubmitPSDistJob(job):
    ret = {}
    dataHandler = DataHandler()
    logging.info("start to submit regular job...")

    try:
        jobParams = json.loads(base64.b64decode(job["jobParams"]))
        jobParams["rest-api"] = config["rest-api"]
        distJobParams = {}
        distJobParams["ps"] = []
        distJobParams["worker"] = []
        assignedRack = None

        if len(config["racks"]) > 0:
            assignedRack = random.choice(config["racks"])

        if jobParams["jobtrainingtype"] == "PSDistJob":
            jobDescriptionList = []
            nums = {"ps":int(jobParams["numps"]),"worker":int(jobParams["numpsworker"])}

            for role in ["ps","worker"]:
                for i in range(nums[role]):
                    distJobParam=copy.deepcopy(jobParams)
                    distJobParam["distId"] = "%s%d" % (role,i)
                    distJobParam["distRole"] = role

                    if "jobPath" not in distJobParam or len(distJobParam["jobPath"].strip()) == 0: 
                        dataHandler.SetJobError(distJobParam["jobId"],"ERROR: job-path does not exist")
                        return False

                    distJobParam["distJobPath"] = os.path.join(distJobParam["jobPath"],distJobParam["distId"])

                    if "workPath" not in distJobParam or len(distJobParam["workPath"].strip()) == 0: 
                        dataHandler.SetJobError(distJobParam["jobId"],"ERROR: work-path does not exist")
                        return False

                    if "dataPath" not in distJobParam or len(distJobParam["dataPath"].strip()) == 0: 
                        dataHandler.SetJobError(distJobParam["jobId"],"ERROR: data-path does not exist")
                        return False

                    jobPath,workPath,dataPath = GetStoragePath(distJobParam["distJobPath"],distJobParam["workPath"],distJobParam["dataPath"])

                    localJobPath = os.path.join(config["storage-mount-path"],jobPath)
                    if not os.path.exists(localJobPath):
                        if "userId" in distJobParam:
                            mkdirsAsUser(localJobPath,distJobParam["userId"])
                        else:
                            mkdirsAsUser(localJobPath,0)


                    distJobParam["LaunchCMD"] = ""
                    if "cmd" not in distJobParam:
                        distJobParam["cmd"] = ""

################One choice is that we only wait for certain time.            
#                    launchCMD = """
##!/bin/bash
#mkdir -p /opt
#echo "[DLWorkspace System]: Waiting for all containers are ready..."
## wait for at most 10 mins. 
#for i in {1..200}; do
#    if [ ! -f /opt/run_dist_job ] || [ ! -f /opt/run_dist_job.sh ]; then
#        sleep 3
#    else
#        break
#    fi
#done
#if [ ! -f /opt/run_dist_job ] || [ ! -f /opt/run_dist_job.sh ]; then
#    echo "[DLWorkspace System]: Waiting for containers: timeout! Restarting..."
#    exit 1
#else
#    echo "[DLWorkspace System]: All containers are ready, launching training job..."
#    chmod +x /opt/run_dist_job.sh
#    /opt/run_dist_job.sh
#fi
#"""


                    launchCMD = """
#!/bin/bash
mkdir -p /opt
echo "[DLWorkspace System]: Waiting for all containers are ready..."
while [ ! -f /opt/run_dist_job ] || [ ! -f /opt/run_dist_job.sh ]; do
    sleep 3
done
echo "[DLWorkspace System]: All containers are ready, launching training job..."
chmod +x /opt/run_dist_job.sh
/opt/run_dist_job.sh
"""

                    launchScriptPath = os.path.join(localJobPath,"launch-%s.sh" % distJobParam["jobId"])
                    with open(launchScriptPath, 'w') as f:
                        f.write(launchCMD)

                    f.close()        
                    distJobParam["LaunchCMD"] = "[\"bash\", \"/job/launch-%s.sh\"]" % distJobParam["jobId"]



                    distJobParam["jobNameLabel"] = ''.join(e for e in distJobParam["jobName"] if e.isalnum())
                    distJobParam["userNameLabel"] = getAlias(jobParams["userName"])
                    ENV = Environment(loader=FileSystemLoader("/"))

                    jobTempDir = os.path.join(config["root-path"],"Jobs_Templete")
                    jobTemp = os.path.join(jobTempDir, "DistJob.yaml.template")

                    distJobParam["hostjobPath"] = os.path.join(config["storage-mount-path"], jobPath)
                    distJobParam["hostworkPath"] = os.path.join(config["storage-mount-path"], workPath)
                    distJobParam["hostdataPath"] = os.path.join(config["storage-mount-path"], dataPath)
                    distJobParam["nvidiaDriverPath"] = nvidiaDriverPath

                    if "mountpoints" not in distJobParam:
                        distJobParam["mountpoints"] = []

                    # distJobParam["mountpoints"].append({"name":"nvidia-driver","containerPath":"/usr/local/nvidia","hostPath":nvidiaDriverPath})
                    distJobParam["mountpoints"].append({"name":"job","containerPath":"/job","hostPath":distJobParam["hostjobPath"]})
                    distJobParam["mountpoints"].append({"name":"work","containerPath":"/work","hostPath":distJobParam["hostworkPath"]})
                    distJobParam["mountpoints"].append({"name":"data","containerPath":"/data","hostPath":distJobParam["hostdataPath"]})
                    distJobParam["pod_ip_range"] = config["pod_ip_range"]
                    if "usefreeflow" in config and config["usefreeflow"] == "True":
                        distJobParam["usefreeflow"] = config["usefreeflow"]
                    else:
                        distJobParam["usefreeflow"] = False


                    random.seed(datetime.datetime.now())
                    distJobParam["containerPort"] = int(random.random()*1000+3000)

                    if assignedRack is not None:
                        if "nodeSelector" not in distJobParam:
                            distJobParam["nodeSelector"] = {}
                        distJobParam["nodeSelector"]["rack"] = assignedRack

                    template = ENV.get_template(os.path.abspath(jobTemp))
                    job_description = template.render(job=distJobParam)

                    jobDescriptionList.append(job_description)

                    distJobParams[role].append(distJobParam)

            jobParams["jobDescriptionPath"] = "jobfiles/" + time.strftime("%y%m%d") + "/" + jobParams["jobId"] + "/" + jobParams["jobId"] + ".yaml"
            jobDescription = "\n---\n".join(jobDescriptionList)


        jobDescriptionPath = os.path.join(config["storage-mount-path"], jobParams["jobDescriptionPath"])
        if not os.path.exists(os.path.dirname(os.path.realpath(jobDescriptionPath))):
            os.makedirs(os.path.dirname(os.path.realpath(jobDescriptionPath)))

        if os.path.isfile(jobDescriptionPath):
            output = k8sUtils.kubectl_delete(jobDescriptionPath) 
            logging.info("kubectl delete " + jobDescriptionPath + " output: " + str(output))

        with open(jobDescriptionPath, 'w') as f:
            f.write(jobDescription)

        output = k8sUtils.kubectl_create(jobDescriptionPath)    
        logging.info("kubectl create " + jobDescriptionPath + " output: " + str(output))

        ret["output"] = output
        ret["jobId"] = jobParams["jobId"]


        if "userName" not in jobParams:
            jobParams["userName"] = ""

        dataHandler.UpdateJobTextField(jobParams["jobId"],"jobStatus","scheduling")
        dataHandler.UpdateJobTextField(jobParams["jobId"],"jobDescriptionPath",jobParams["jobDescriptionPath"])
        dataHandler.UpdateJobTextField(jobParams["jobId"],"jobDescription",base64.b64encode(jobDescription))


        jobMeta = {}
        jobMeta["jobDescriptionPath"] = jobParams["jobDescriptionPath"]
        jobMeta["jobPath"] = jobParams["jobPath"]
        jobMeta["workPath"] = jobParams["workPath"]
        jobMeta["jobPath"] = jobParams["jobPath"]
        jobMeta["LaunchCMD"] = jobParams["LaunchCMD"]
        jobMeta["distJobParams"] = distJobParams

        jobMetaStr = base64.b64encode(json.dumps(jobMeta))
        dataHandler.UpdateJobTextField(jobParams["jobId"],"jobMeta",jobMetaStr)

    except Exception as e:
        print e
        ret["error"] = str(e)
        retries = dataHandler.AddandGetJobRetries(jobParams["jobId"])
        if retries >= 5:
            dataHandler.UpdateJobTextField(jobParams["jobId"],"jobStatus","error")
            dataHandler.UpdateJobTextField(jobParams["jobId"],"errorMsg","Cannot submit job!" + str(e))

    return ret

def KillJob(job):
    dataHandler = DataHandler()
    result, detail = k8sUtils.GetJobStatus(job["jobId"])
    dataHandler.UpdateJobTextField(job["jobId"],"jobStatusDetail",base64.b64encode(json.dumps(detail)))

    msg = "Killing job %s, with status %s, %s" %(job["jobId"], result,detail)
    logging.info(msg)

    if "jobDescriptionPath" in job and job["jobDescriptionPath"] is not None:
        jobDescriptionPath = os.path.join(config["storage-mount-path"], job["jobDescriptionPath"])

        if os.path.isfile(jobDescriptionPath):

            code = k8sUtils.kubectl_delete(jobDescriptionPath)
            if  code == 0:
                logging.info("kubectl delete " + jobDescriptionPath + " succ. output: 0")
                dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","killed")
                return True

            else:
                dataHandler.UpdateJobTextField(job["jobId"],"errorMsg","Cannot delete job from Kubernetes Cluster!")
                logging.info("kubectl delete " + jobDescriptionPath + " failed. output: " + str(code))
                
    else:
        dataHandler.UpdateJobTextField(job["jobId"],"errorMsg","Cannot find job description file!")

    dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","error")
    return False


def getAlias(username):
    if "@" in username:
        username = username.split("@")[0].strip()

    if "/" in username:
        username = username.split("/")[1].strip()

    return username


def ApproveJob(job):
    logging.info("start to Approve job...")

    dataHandler = DataHandler()
    dataHandler.ApproveJob(job["jobId"])
    dataHandler.Close()
    return True

def AutoApproveJob(job):

    cluster_status = get_cluster_status()
    jobUser = getAlias(job["userName"])
    jobParams = json.loads(base64.b64decode(job["jobParams"]))
    jobGPU = int(jobParams["resourcegpu"])

    logging.info("start to autoApprove job...")
    currentGPU = 0
    logging.info("currentGPU: " + str(currentGPU) + " jobGPU: " + str(jobGPU))

    for user in cluster_status["user_status"]:
        if user["userName"] == jobUser:
            currentGPU = int(user["userGPU"])

    # Default Auto approval changed to 2GPU
    # if currentGPU == 0 or currentGPU + jobGPU <= 2:
    if currentGPU + jobGPU <= 1:
        ApproveJob(job)


UnusualJobs = {}

def UpdateJobStatus(job):

    dataHandler = DataHandler()
    jobParams = json.loads(base64.b64decode(job["jobParams"]))
    logging.info("start to update job status...")

    if job["jobStatus"] == "scheduling" and jobParams["jobtrainingtype"] == "PSDistJob":
        launch_ps_dist_job(jobParams)

    jobPath,workPath,dataPath = GetStoragePath(jobParams["jobPath"],jobParams["workPath"],jobParams["dataPath"])
    localJobPath = os.path.join(config["storage-mount-path"],jobPath)
    logPath = os.path.join(localJobPath,"logs/joblog.txt")    

    result, detail = k8sUtils.GetJobStatus(job["jobId"])
    dataHandler.UpdateJobTextField(job["jobId"],"jobStatusDetail",base64.b64encode(json.dumps(detail)))

    msg = "job %s status, result: %s, detail: %s" % (job["jobId"], result, json.dumps(detail))
    logging.info(msg)
    
    jobDescriptionPath = os.path.join(config["storage-mount-path"], job["jobDescriptionPath"]) if "jobDescriptionPath" in job else None
    
    if "userId" not in jobParams:
        jobParams["userId"]    = "0"

    if result.strip() == "Succeeded":
        joblog_manager.extract_job_log(job["jobId"],logPath,jobParams["userId"])
        dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","finished")

        if jobDescriptionPath is not None and os.path.isfile(jobDescriptionPath):
            output = k8sUtils.kubectl_delete(jobDescriptionPath) 
            logging.info("kubectl delete " + jobDescriptionPath + " output: " + str(output))

    elif result.strip() == "Running":
        if job["jobStatus"] != "running":
            dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","running")

        if "interactivePort" in jobParams:
            serviceAddress = k8sUtils.GetServiceAddress(job["jobId"])
            serviceAddress = base64.b64encode(json.dumps(serviceAddress))
            dataHandler.UpdateJobTextField(job["jobId"],"endpoints",serviceAddress)

    elif result.strip() == "Failed":
        printlog("Job %s fails, cleaning..." % job["jobId"])
        joblog_manager.extract_job_log(job["jobId"],logPath,jobParams["userId"])
        dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","failed")
        dataHandler.UpdateJobTextField(job["jobId"],"errorMsg",detail)

        if jobDescriptionPath is not None and os.path.isfile(jobDescriptionPath):
            output = k8sUtils.kubectl_delete(jobDescriptionPath) 
            logging.info("kubectl delete " + jobDescriptionPath + " output: " + str(output))

    elif result.strip() == "Unknown":
        if job["jobId"] not in UnusualJobs:
            UnusualJobs[job["jobId"]] = datetime.datetime.now()

        elif (datetime.datetime.now() - UnusualJobs[job["jobId"]]).seconds > 300:
            del UnusualJobs[job["jobId"]]

            retries = dataHandler.AddandGetJobRetries(job["jobId"])
            if retries >= 5:
                printlog("Job %s fails for more than 5 times, abort" % job["jobId"])
                dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","error")
                dataHandler.UpdateJobTextField(job["jobId"],"errorMsg","cannot launch the job.")

                if jobDescriptionPath is not None and os.path.isfile(jobDescriptionPath):
                    output = k8sUtils.kubectl_delete(jobDescriptionPath)     
                    logging.info("kubectl delete " + jobDescriptionPath + " output: " + str(output))

            else:
                printlog("Job %s fails in Kubernetes, delete and re-submit the job. Retries %d" % (job["jobId"] , retries))
                SubmitJob(job)

    elif result.strip() == "PendingHostPort":
        printlog("Cannot find host ports for job :%s, re-launch the job with different host ports " % (job["jobId"]))
    
        SubmitJob(job)

    if result.strip() != "Unknown" and job["jobId"] in UnusualJobs:
        del UnusualJobs[job["jobId"]]

def UpdateDistJobStatus(job):
    dataHandler = DataHandler()
    jobParams = json.loads(base64.b64decode(job["jobParams"]))

    if "userId" not in jobParams:
        jobParams["userId"]    = "0"

    jobPath,workPath,dataPath = GetStoragePath(jobParams["jobPath"],jobParams["workPath"],jobParams["dataPath"])
    localJobPath = os.path.join(config["storage-mount-path"],jobPath)
    logPath = os.path.join(localJobPath,"logs/joblog.txt")
    

    result, detail = k8sUtils.GetJobStatus(job["jobId"])
    dataHandler.UpdateJobTextField(job["jobId"],"jobStatusDetail",base64.b64encode(detail))

    msg = "job %s status. result: %s, detail: %s" % (job["jobId"], result, json.dumps(detail))
    logging.info(msg)
    jobDescriptionPath = os.path.join(config["storage-mount-path"], job["jobDescriptionPath"]) if "jobDescriptionPath" in job else None


    jobId = jobParams["jobId"]
    workerPodInfo = k8sUtils.GetPod("distRole=worker,run=" + jobId)
    psPodInfo = k8sUtils.GetPod("distRole=ps,run=" + jobId)

    if "items" in workerPodInfo and len(workerPodInfo["items"]) == int(jobParams["numpsworker"]) and "items" in psPodInfo and len(psPodInfo["items"]) == int(jobParams["numps"]):
        if job["jobStatus"] == "scheduling" :
            launch_ps_dist_job(jobParams)
        if job["jobStatus"] == "running":
            result, detail = GetDistJobStatus(job["jobId"])
            dataHandler.UpdateJobTextField(job["jobId"],"jobStatusDetail",base64.b64encode(detail))

            printlog("job %s status: %s" % (job["jobId"], result))
    
            jobDescriptionPath = os.path.join(config["storage-mount-path"], job["jobDescriptionPath"]) if "jobDescriptionPath" in job else None

            if result.strip() == "Succeeded":
                joblog_manager.extract_job_log(job["jobId"],logPath,jobParams["userId"])
                dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","finished")

                if jobDescriptionPath is not None and os.path.isfile(jobDescriptionPath):
                    output = k8sUtils.kubectl_delete(jobDescriptionPath) 
                    logging.info("kubectl delete " + jobDescriptionPath + " output: " + str(output))

            elif result.strip() == "Running":
                joblog_manager.extract_job_log(job["jobId"],logPath,jobParams["userId"])

                if job["jobStatus"] != "running":
                    dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","running")

                if "interactivePort" in jobParams:
                    serviceAddress = k8sUtils.GetServiceAddress(job["jobId"])
                    serviceAddress = base64.b64encode(json.dumps(serviceAddress))
                    dataHandler.UpdateJobTextField(job["jobId"],"endpoints",serviceAddress)

            elif result.strip() == "Failed":
                printlog("Job %s fails, cleaning..." % job["jobId"])
                joblog_manager.extract_job_log(job["jobId"],logPath,jobParams["userId"])
                dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","failed")
                dataHandler.UpdateJobTextField(job["jobId"],"errorMsg",detail)

                if jobDescriptionPath is not None and os.path.isfile(jobDescriptionPath):
                    output = k8sUtils.kubectl_delete(jobDescriptionPath) 
                    logging.info("kubectl delete " + jobDescriptionPath + " output: " + str(output))

            elif result.strip() == "Unknown":
                if job["jobId"] not in UnusualJobs:
                    UnusualJobs[job["jobId"]] = datetime.datetime.now()
                elif (datetime.datetime.now() - UnusualJobs[job["jobId"]]).seconds > 300:
                    del UnusualJobs[job["jobId"]]
                    retries = dataHandler.AddandGetJobRetries(job["jobId"])
                    if retries >= 5:
                        printlog("Job %s fails for more than 5 times, abort" % job["jobId"])
                        dataHandler.UpdateJobTextField(job["jobId"],"jobStatus","error")
                        dataHandler.UpdateJobTextField(job["jobId"],"errorMsg","cannot launch the job.")

                        if jobDescriptionPath is not None and os.path.isfile(jobDescriptionPath):
                            output = k8sUtils.kubectl_delete(jobDescriptionPath)               
                            logging.info("kubectl delete " + jobDescriptionPath + " output: " + str(output))
  
                    else:
                        printlog("Job %s fails in Kubernetes, delete and re-submit the job. Retries %d" % (job["jobId"] , retries))
                        SubmitJob(job)

            if result.strip() != "Unknown" and job["jobId"] in UnusualJobs:
                del UnusualJobs[job["jobId"]]

    pass


def run_dist_cmd_on_pod(podId, cmd, outputfile):
    remotecmd = "exec %s -- %s" % (podId,cmd)
    print remotecmd

    k8sUtils.kubectl_exec_output_to_file(remotecmd,outputfile)
    logging.info("kubectl exec " + remotecmd)
    return


class Kube_RemoteCMD_Thread(threading.Thread):
    def __init__(self, jobId, podId, cmd, outputfile):
        threading.Thread.__init__(self)
        self.jobId = jobId
        self.podId = podId
        self.cmd = cmd
        self.outputfile = outputfile
    def run(self):
        run_dist_cmd_on_pod(self.podId, self.cmd, self.outputfile)


def launch_ps_dist_job(jobParams):
    jobId = jobParams["jobId"]
    workerPodInfo = k8sUtils.GetPod("distRole=worker,run=" + jobId)
    psPodInfo = k8sUtils.GetPod("distRole=ps,run=" + jobId)

    if "items" in workerPodInfo and len(workerPodInfo["items"]) == int(jobParams["numpsworker"]) and "items" in psPodInfo and len(psPodInfo["items"]) == int(jobParams["numps"]):
        podStatus = [k8sUtils.check_pod_status(pod) for pod in  workerPodInfo["items"] + psPodInfo["items"] ]

        if all([status == "Running" for status in podStatus]):
            ps_pod_names = [pod["metadata"]["name"] for pod in psPodInfo["items"]]
            worker_pod_names = [pod["metadata"]["name"] for pod in workerPodInfo["items"]]

            ps_pod_ips = [pod["status"]["podIP"] for pod in psPodInfo["items"]]
            worker_pod_ips = [pod["status"]["podIP"] for pod in workerPodInfo["items"]]

            ps_num = len(psPodInfo["items"])
            worker_num = len(workerPodInfo["items"])

            ps_ports = [int(item["metadata"]["labels"]["distPort"]) for item in psPodInfo["items"]]
            worker_ports = [int(item["metadata"]["labels"]["distPort"]) for item in workerPodInfo["items"]]

            #port range: 30000~31000
            #rndList = range(max(1000,ps_num + worker_num))
            #random.shuffle(rndList)
            #ps_ports = [rndList[i] + 30000 for i in range(ps_num)]
            #worker_ports = [rndList[i + ps_num] + 30000 for i in range(worker_num)]

            ps_hosts = ",".join(["%s:%s" % (ps_pod_ips[i],ps_ports[i]) for i in range(ps_num)])
            worker_hosts = ",".join(["%s:%s" % (worker_pod_ips[i],worker_ports[i]) for i in range(worker_num)])

            ps_files = ["/tmp/" + str(uuid.uuid4()) for i in range(ps_num)]
            worker_files = ["/tmp/" + str(uuid.uuid4()) for i in range(worker_num)]

            ps_cmd = ["%s --ps_hosts=%s --worker_hosts=%s --job_name=ps --task_index=%d 2>&1 | tee %s" % (jobParams["cmd"], ps_hosts,worker_hosts,i,ps_files[i]) for i in range(ps_num)]
            worker_cmd = ["%s --ps_hosts=%s --worker_hosts=%s --job_name=worker --task_index=%d 2>&1 | tee %s" % (jobParams["cmd"], ps_hosts,worker_hosts,i,worker_files[i]) for i in range(worker_num)]


            for i in range(ps_num):
                os.system("mkdir -p %s" % ps_files[i])
                ps_files[i] = os.path.join(ps_files[i],"run_dist_job.sh")

                with open(ps_files[i], 'w') as f:
                    f.write(ps_cmd[i] + "\n")

                f.close()  

                if "userId" in jobParams:
                    os.system("chown -R %s %s" % (jobParams["userId"], ps_files[i]))

                remotecmd = "cp %s %s:/opt/run_dist_job.sh" % (ps_files[i],ps_pod_names[i])
                output = k8sUtils.kubectl_exec(remotecmd)
                logging.info("kubectl exec: " + remotecmd + " output: " + str(output))

                remotecmd = "exec %s touch /opt/run_dist_job" % ps_pod_names[i]
                output = k8sUtils.kubectl_exec(remotecmd)
                logging.info("kubectl exec: " + remotecmd + " output: " + str(output))


            for i in range(worker_num):
                os.system("mkdir -p %s" % worker_files[i])
                worker_files[i] = os.path.join(worker_files[i],"run_dist_job.sh")

                with open(worker_files[i], 'w') as f:
                    f.write(worker_cmd[i] + "\n")

                f.close()    
                if "userId" in jobParams:
                    os.system("chown -R %s %s" % (jobParams["userId"], worker_files[i]))

                remotecmd = "cp %s %s:/opt/run_dist_job.sh" % (worker_files[i],worker_pod_names[i])
                output = k8sUtils.kubectl_exec(remotecmd)
                logging.info("kubectl exec: " + remotecmd + " output: " + str(output))

                remotecmd = "exec %s touch /opt/run_dist_job" % worker_pod_names[i]
                output = k8sUtils.kubectl_exec(remotecmd)
                logging.info("kubectl exec: " + remotecmd + " output: " + str(output))


            dataHandler = DataHandler()
            dataHandler.UpdateJobTextField(jobParams["jobId"],"jobStatus","running")

            #ps_threads = [Kube_RemoteCMD_Thread(jobId,ps_pod_names[i],ps_cmd[i],ps_logfiles[i]) for i in range(ps_num)]
            #worker_threads = [Kube_RemoteCMD_Thread(jobId,worker_pod_names[i],worker_cmd[i],worker_logfiles[i]) for i in range(worker_num)]
            
            #for t in ps_threads:
            #    t.start()

            #for t in worker_threads:
            #    t.start()


            #while (True):
                #for t in ps_threads:
                #    print t.isAlive()
                #time.sleep(5)

            #cmd = "test"
            #thread.start_new_thread( run_dist_cmd_on_pod,
            #(workerPodInfo["items"][0]["metadata"]["name"], cmd) )




def create_log( logdir = '/var/log/dlworkspace' ):

    if not os.path.exists( logdir ):
        os.system("mkdir -p " + logdir )

    with open('logging.yaml') as f:
        logging_config = yaml.load(f)
        f.close()
        logging_config["handlers"]["file"]["filename"] = logdir+"/jobmanager.log"
        logging.config.dictConfig(logging_config)


def Run():
    create_log()
    logging.info("start to process jobs ...")

    while True:

        try:
            config["racks"] = k8sUtils.get_node_labels("rack")
            config["skus"] = k8sUtils.get_node_labels("sku")

        except Exception as e:
            print e

        try:
            dataHandler = DataHandler()
            pendingJobs = dataHandler.GetPendingJobs()
            #printlog("updating status for %d jobs" % len(pendingJobs))

            for job in pendingJobs:
                try:
                    logging.info("to process one pendingJob.")
                    msg = "Processing job: %s, status: %s" % (str(job["jobId"]), str(job["jobStatus"]))
                    logging.info(msg)
                    
                    if job["jobStatus"] == "queued":
                        SubmitJob(job)

                    elif job["jobStatus"] == "killing":
                        KillJob(job)

                    elif job["jobStatus"] == "scheduling" or job["jobStatus"] == "running" :
                        UpdateJobStatus(job)

                    elif job["jobStatus"] == "unapproved" :
                        AutoApproveJob(job)

                except Exception as e:
                    print e

        except Exception as e:
            print e

        time.sleep(1)

if __name__ == '__main__':
    Run()
    #print k8sUtils.get_pod_events("d493d41c-45ea-4e85-8ca4-01c3533cd727")
