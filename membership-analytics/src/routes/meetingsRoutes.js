const express = require('express');
const controller = require('../controllers/meetingsController');

const router = express.Router();

router.get('/filters', controller.filters);
router.get('/overview', controller.overview);
router.get('/list', controller.list);

module.exports = router;
