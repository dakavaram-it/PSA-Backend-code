const express = require('express');
const controller = require('../controllers/sirDashboardController');

const router = express.Router();

router.get('/overview', controller.overview);
router.get('/parliament', controller.parliament);
router.get('/assembly', controller.assembly);
router.get('/cubs/overview', controller.cubsOverview);
router.get('/cubs/parliament', controller.cubsParliament);
router.get('/cubs/assembly', controller.cubsAssembly);

module.exports = router;
