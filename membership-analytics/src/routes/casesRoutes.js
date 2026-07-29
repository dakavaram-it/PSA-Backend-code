const express = require('express');
const controller = require('../controllers/casesController');

const router = express.Router();

router.get('/geo', controller.geo);
router.get('/overview', controller.overview);
router.get('/leaders', controller.leaders);
router.get('/search', controller.search);
// leaderKey contains ':' (e.g. c:123) -> use wildcard to keep it intact
router.get('/leaders/:key', controller.leaderDetail);

module.exports = router;
