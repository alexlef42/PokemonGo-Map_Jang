/*eslint no-unused-vars: ["error", { "varsIgnorePattern": "initMap" }]*/
/*global google */
/*global centerLat */
/*global centerLng */
/*global $ */

var map
var markers = {}
var circles = {}
var polygons = {}
var gPolygonId = 0

function initMap () {

  google.maps.LatLng.prototype.destinationPoint = function(brng, dist) {
   dist = dist / 6371;
   brng = brng.toRad();

   var lat1 = this.lat().toRad(), lon1 = this.lng().toRad();

   var lat2 = Math.asin(Math.sin(lat1) * Math.cos(dist) +
                        Math.cos(lat1) * Math.sin(dist) * Math.cos(brng));

   var lon2 = lon1 + Math.atan2(Math.sin(brng) * Math.sin(dist) *
                                Math.cos(lat1),
                                Math.cos(dist) - Math.sin(lat1) *
                                Math.sin(lat2));

   if (isNaN(lat2) || isNaN(lon2)) return null;

   return new google.maps.LatLng(lat2.toDeg(), lon2.toDeg());
  }

  map = new google.maps.Map(document.getElementById('map'), {
    center: {
      lat: centerLat,
      lng: centerLng
    },
    zoom: 16,
    fullscreenControl: true,
    streetViewControl: false,
    mapTypeControl: false,
    mapTypeControlOptions: {
      style: google.maps.MapTypeControlStyle.DROPDOWN_MENU,
      position: google.maps.ControlPosition.RIGHT_TOP,
      mapTypeIds: [
        google.maps.MapTypeId.ROADMAP,
        google.maps.MapTypeId.SATELLITE
      ]
    }
  })

  // google.maps.event.addListener(map, 'click', function (event) {
  //   gMarkerid++
  //   markers[gMarkerid] = placeMarker(event.latLng, gMarkerid)
  // })


  var drawingManager = new google.maps.drawing.DrawingManager({
    drawingMode: google.maps.drawing.OverlayType.POLYGON,
    drawingControl: true,
    drawingControlOptions: {
      position: google.maps.ControlPosition.TOP_CENTER,
      drawingModes: ['polygon']
    },
    polygonOptions: {
      editable: true,
      zIndex: 100
    }
  });
  drawingManager.setMap(map);

  google.maps.event.addListener(drawingManager, 'polygonupdate', function(poly) {
    console.log(poly)
  });

  google.maps.event.addListener(drawingManager, 'polygoncomplete', function(poly) {

    gPolygonId++;
    poly.id = gPolygonId;
    var vertices = poly.getPath();

    var redraw = function(id) {
      return function() {
        console.log('re drawing ' + id)
      }
    }(gPolygonId);

    poly.getPaths().forEach(function(path, index){
      google.maps.event.addListener(path, 'insert_at', redraw);
      google.maps.event.addListener(path, 'remove_at', redraw);
      google.maps.event.addListener(path, 'set_at', redraw);
    });

    var lats = [];
    var lngs = [];
    for (var i =0; i < vertices.getLength(); i++) {
      var xy = vertices.getAt(i);
      lats.push(xy.lat());
      lngs.push(xy.lng());
    }
    new google.maps.Rectangle({
      strokeColor: '#FF0000',
      strokeOpacity: 0.3,
      strokeWeight: 2,
      fillColor: '#FF0000',
      fillOpacity: 0.05,
      map: map,
      bounds: {
        north: lats.max(),
        south: lats.min(),
        east: lngs.max(),
        west: lngs.min()
      },
      zIndex: 1
    });

    // cover it in circles
    var curLat = lats.min();
    var curLng = lngs.min();
    var radiusInKm = 140 / 1000;
    while (curLng < lngs.max()) {
      while (curLat < lats.max()) {
        var pointA = new google.maps.LatLng(curLat, curLng);
        var pointB = pointA.destinationPoint(90, radiusInKm/2).destinationPoint(0, radiusInKm/2);
        if (google.maps.geometry.poly.containsLocation(pointA, poly)) {
          circ(pointA);
        }
        if (google.maps.geometry.poly.containsLocation(pointB, poly)) {
          circ(pointB);
        }
        var nextPoint = pointA.destinationPoint(0, radiusInKm);
        curLat = nextPoint.lat();
      }
      curLat = lats.min();
      var pointA = new google.maps.LatLng(curLat, curLng);
      curLng = pointA.destinationPoint(90, radiusInKm).lng();
    }
  });
}

function circ(point) {
    return new google.maps.Circle({
      strokeColor: '#00FF00',
      strokeOpacity: 0.8,
      strokeWeight: 1,
      fillColor: '#00FF00',
      fillOpacity: 0.35,
      map: map,
      center: point,
      radius: 70,
      zIndex: 1
    });
}

Array.prototype.max = function() {
  return Math.max.apply(null, this);
};

Array.prototype.min = function() {
  return Math.min.apply(null, this);
};

Number.prototype.toRad = function() {
   return this * Math.PI / 180;
}

Number.prototype.toDeg = function() {
   return this * 180 / Math.PI;
}



// function placeMarker (location, markerid) {
//   var marker = new google.maps.Marker({
//     position: location,
//     map: map,
//     draggable: true,
//     clickable: true
//   })
//   marker.addListener('drag', function () { clearCircles(markerid) })
//   marker.addListener('dragend', function (event) { genCircles(event.latLng, 5, markerid) })
//   genCircles(location, 5, markerid)
//   return marker
// }

function genCircles (location, steps, markerid) {
  $.ajax({
    url: 'beehive-calc',
    type: 'GET',
    data: {
      'lat': location.lat(),
      'lng': location.lng(),
      'steps': steps,
      'markerid': markerid
    },
    dataType: 'json',
    cache: false
  }).done(function (result) {
    var circleSet = []
    $.each(result.steps, function (index, value) {
      circleSet.push(setupScannedMarker(value[0], value[1]))
    })
    circles[result.markerid] = circleSet
  })
}

function clearCircles (markerid) {
  $.each(circles[markerid], function (index, value) {
    value.setMap(null)
  })
}

function setupScannedMarker (lat, lng) {
  var marker = new google.maps.Circle({
    map: map,
    clickable: false,
    center: new google.maps.LatLng(lat, lng),
    radius: 70,
    fillColor: '#cccccc',
    strokeWeight: 1
  })
  return marker
}
