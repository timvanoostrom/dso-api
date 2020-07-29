#!groovy

def tryStep(String message, Closure block, Closure tearDown = null) {
    try {
        block()
    }
    catch (Throwable t) {
        slackSend message: "${env.JOB_NAME}: ${message} failure ${env.BUILD_URL}", channel: '#ci-channel', color: 'danger'
        throw t
    }
    finally {
        if (tearDown) {
            tearDown()
        }
    }
}

node {
    stage("Checkout") {
        checkout scm
    }

    stage('Test') {
        tryStep "test", {
            sh "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml build --pull && " +
               "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml run -u root --rm test"
        }, {
            sh "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml down"
        }
    }

    // The rebuilding likely reuses the build cache from docker-compose.
    stage("Build API image") {
        tryStep "build", {
            docker.withRegistry("${DOCKER_REGISTRY_HOST}",'docker_registry_auth') {
            def image = docker.build("datapunt/dataservices/dso-api:${env.BUILD_NUMBER}", "src")
            image.push()
            }
        }
    }

    // The rebuilding likely reuses the build cache from docker-compose.
    stage("Build API-Docs image") {
        tryStep "build docs", {
            docker.withRegistry("${DOCKER_REGISTRY_HOST}",'docker_registry_auth') {
                def image = docker.build("datapunt/dataservices/dso-api-docs:${env.BUILD_NUMBER}", "docs")
                sh "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml up -d &&" +
                    "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml exec -T docs service nginx stop &&" +
                    "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml exec -T test python manage.py migrate &&" +
                    "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml exec -T test python manage.py import_schemas --no-create-tables &&" +
                    "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml exec -T test python manage.py change_dataset brp --endpoint-url=https://api.data.amsterdam.nl &&" +
                    "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml exec -T test python manage.py import_schemas &&" +
                    "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml exec -T docs make datasets-docker &&" +
                    "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml exec -T docs service nginx start &&" +
                    "docker commit \$(docker ps -aqf 'name=dso_api_docs') datapunt/dataservices/dso-api-docs:${env.BUILD_NUMBER}"

                image.push()
            }
        }, {
            sh "docker-compose -p dso_api -f src/.jenkins/test/docker-compose.yml down"
        }
    }
}


String BRANCH = "${env.BRANCH_NAME}"

if (BRANCH == "master") {

    node {
        stage('Push acceptance image') {
            tryStep "image tagging", {
                docker.withRegistry("${DOCKER_REGISTRY_HOST}",'docker_registry_auth') {
                    def image = docker.image("datapunt/dataservices/dso-api:${env.BUILD_NUMBER}")
                    image.pull()
                    image.push("acceptance")
                }
            }
            tryStep "docs image tagging", {
                docker.withRegistry("${DOCKER_REGISTRY_HOST}",'docker_registry_auth') {
                    def image = docker.image("datapunt/dataservices/dso-api-docs:${env.BUILD_NUMBER}")
                    image.pull()
                    image.push("acceptance")
                }
            }
        }
    }

    node {
        stage("Deploy to ACC") {
            tryStep "deployment", {
                build job: 'Subtask_Openstack_Playbook',
                parameters: [
                    [$class: 'StringParameterValue', name: 'INVENTORY', value: 'acceptance'],
                    [$class: 'StringParameterValue', name: 'PLAYBOOK', value: 'deploy-dso-api.yml'],
                ]
            }
        }
    }

    stage('Waiting for approval') {
        slackSend channel: '#ci-channel', color: 'warning', message: 'DSO-API is waiting for Production Release - please confirm'
        input "Deploy to Production?"
    }

    node {
        stage('Push production image') {
            tryStep "image tagging", {
                docker.withRegistry("${DOCKER_REGISTRY_HOST}",'docker_registry_auth') {
                    def image = docker.image("datapunt/dataservices/dso-api:${env.BUILD_NUMBER}")
                    image.pull()
                    image.push("production")
                    image.push("latest")
                }
            }
            tryStep "docs image tagging", {
                docker.withRegistry("${DOCKER_REGISTRY_HOST}",'docker_registry_auth') {
                    def image = docker.image("datapunt/dataservices/dso-api-docs:${env.BUILD_NUMBER}")
                    image.pull()
                    image.push("production")
                    image.push("latest")
                }
            }
        }
    }

    node {
        stage("Deploy") {
            tryStep "deployment", {
                build job: 'Subtask_Openstack_Playbook',
                parameters: [
                    [$class: 'StringParameterValue', name: 'INVENTORY', value: 'production'],
                    [$class: 'StringParameterValue', name: 'PLAYBOOK', value: 'deploy-dso-api.yml'],
                ]
            }
        }
    }

}
