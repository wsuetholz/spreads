/* jshint unused: false */
/* global console: true */
'use(strict)';

function ConfigCtrl($scope, $http, ConfigService) {
    $scope.configuration = ConfigService;
    $scope.submit = function() {
        console.debug('Sending configuration to server');
        $http.put('/api/configuration', ConfigService);
    };
}

function StatusCtrl($scope, StatusService) {
    $scope.status = StatusService;
}

function StartCtrl($scope, StatusService, $log, $location) {
    $log.info('Trying to redirect to /' + StatusService.currentStep);
    $location.path('/' + StatusService.currentStep);
}

function SetupCtrl($scope, StatusService, $log, $location) {
    StatusService.currentStep = 'setup';
    StatusService.stepNum = 1;

    $scope.next = function() {
        // TODO: Throw error when project name is empty
        console.debug('Project name is ' + $scope.projectName);
        StatusService.projectName = $scope.projectName;
        $scope.submit();
        $location.path('/scan');
    };
}

function ScanCtrl($scope, StatusService, $log, $location) {
    StatusService.currentStep = 'scan';
    StatusService.stepNum = 2;
    $log.info('StatusService.stepNum is: ' + StatusService.stepNum);

    $scope.next = function() {
        $location.path('/download');
    };
}

function DownloadCtrl($scope, StatusService, $log, $location) {
    StatusService.currentStep = 'download';
    StatusService.stepNum = 3;

    $scope.next = function() {
        $location.path('/verify');
    };
}

function VerifyCtrl($scope, StatusService, $log, $location) {
    StatusService.currentStep = 'verify';
    StatusService.stepNum = 4;

    $scope.next = function() {
        $location.path('/submit');
    };
}

function SubmitCtrl($scope, StatusService, $log, $location) {
    StatusService.currentStep = 'submit';
    StatusService.stepNum = 5;
    $scope.next = function() {
        $location.path('/setup');
    };
}